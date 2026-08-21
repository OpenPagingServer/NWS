import json
import importlib.util
import os
import re
import sys
import uuid
from pathlib import Path

import pymysql
from dotenv import load_dotenv
from flask import jsonify
import endpoints

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

NWS_COMMON_REQUIRED = (
    "ALERT_ORDER",
    "ALERT_SPECS",
    "VARIABLE_LABELS",
    "clamp_poll_interval",
    "default_entry_for_alert",
    "h",
    "load_zone_catalog_safe",
    "normalize_entry",
    "normalize_groups_value",
    "official_alert_type_names",
    "state_label",
)


def load_nws_common():
    candidates = [
        ROOT_DIR / "nws_common.py",
        ROOT_DIR / "payload" / "nws_common.py",
        BASE_DIR / "nws_common.py",
    ]
    for module_path in candidates:
        if not module_path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("nws_common_runtime_web", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not all(hasattr(module, name) for name in NWS_COMMON_REQUIRED):
            continue
        return module
    raise FileNotFoundError("nws_common.py was not found in the module package")


nws_common = load_nws_common()
ALERT_ORDER = nws_common.ALERT_ORDER
ALERT_SPECS = nws_common.ALERT_SPECS
VARIABLE_LABELS = nws_common.VARIABLE_LABELS
default_entry_for_alert = nws_common.default_entry_for_alert
h = nws_common.h
load_zone_catalog_safe = nws_common.load_zone_catalog_safe
normalize_entry = nws_common.normalize_entry
normalize_groups_value = nws_common.normalize_groups_value
official_alert_type_names = nws_common.official_alert_type_names
state_label = nws_common.state_label
DEFAULT_POLL_INTERVAL = getattr(nws_common, "DEFAULT_POLL_INTERVAL", 180)
DEFAULT_WATCH_POLL_INTERVAL = getattr(nws_common, "DEFAULT_WATCH_POLL_INTERVAL", 60)
clamp_poll_interval = nws_common.clamp_poll_interval

ENV_PATH = ROOT_DIR.parent.parent / ".env"
load_dotenv(ENV_PATH)

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_NAME = os.getenv("DB_NAME")
ENDPOINT_TABLE = "endpoints-input-nws"
ACTIVE_TABLE = "endpoints-input-nws-active"
DRAFT_TABLE = "endpoints-input-nws-drafts"


def db():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def query_all(sql, params=()):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def execute(sql, params=()):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def ensure_draft_table():
    execute(
        f"CREATE TABLE IF NOT EXISTS `{DRAFT_TABLE}` ("
        "`token` VARCHAR(128) NOT NULL, `data` LONGTEXT NOT NULL, "
        "`updated_at` DATETIME NOT NULL, PRIMARY KEY (`token`))"
    )


def load_draft(token):
    ensure_draft_table()
    rows = query_all(f"SELECT `data` FROM `{DRAFT_TABLE}` WHERE `token`=%s LIMIT 1", (token,))
    if not rows:
        return None
    try:
        value = json.loads(rows[0].get("data") or "{}")
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def save_draft(token, draft):
    ensure_draft_table()
    execute(
        f"INSERT INTO `{DRAFT_TABLE}` (`token`,`data`,`updated_at`) VALUES (%s,%s,NOW()) "
        "ON DUPLICATE KEY UPDATE `data`=VALUES(`data`),`updated_at`=NOW()",
        (token, json.dumps(draft, separators=(",", ":"), ensure_ascii=True)),
    )


def delete_draft(token):
    ensure_draft_table()
    execute(f"DELETE FROM `{DRAFT_TABLE}` WHERE `token`=%s", (token,))


def group_rows_for_user(user):
    rows = query_all("SELECT `id`, `name` FROM `groups` ORDER BY `name` ASC, `id` ASC")
    return rows


def ensure_endpoint_columns():
    for column, ddl in (
        ("poll_interval", f"ALTER TABLE `{ENDPOINT_TABLE}` ADD COLUMN `poll_interval` INT NOT NULL DEFAULT {int(DEFAULT_POLL_INTERVAL)}"),
        ("poll_interval_watch", f"ALTER TABLE `{ENDPOINT_TABLE}` ADD COLUMN `poll_interval_watch` INT NOT NULL DEFAULT {int(DEFAULT_WATCH_POLL_INTERVAL)}"),
    ):
        try:
            rows = query_all(
                f"SELECT COUNT(*) AS c FROM information_schema.columns "
                f"WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s",
                (ENDPOINT_TABLE, column),
            )
            if rows and int(rows[0].get("c") or 0):
                continue
            execute(ddl)
        except Exception:
            pass


def endpoint_row(row_id):
    ensure_endpoint_columns()
    rows = query_all(
        f"SELECT `id`, `name`, `enabled`, `groups`, `entries_json`, `poll_interval`, `poll_interval_watch`, `last_checked`, `last_error` FROM `{ENDPOINT_TABLE}` WHERE `id`=%s LIMIT 1",
        (row_id,),
    )
    return rows[0] if rows else None


def parse_entries(raw):
    try:
        loaded = json.loads(raw or "[]")
    except Exception:
        return []
    out = []
    for item in loaded if isinstance(loaded, list) else []:
        try:
            out.append(normalize_entry(item))
        except Exception:
            continue
    return out


PRIORITY_RANK = {"Emergency": 4, "High": 3, "Normal": 2, "Low": 1}


def alert_options():
    specs = [ALERT_SPECS[key] for key in ALERT_ORDER]
    return sorted(
        specs,
        key=lambda spec: (-PRIORITY_RANK.get(str(spec.get("priority") or ""), 0), ALERT_ORDER.index(spec["id"])),
    )


def forms():
    return {
        "endpoint": {
            "label": "NWS Alert Endpoint",
            "description": "Monitor National Weather Service alerts and send OPS messages by county or marine zone.",
        }
    }


def endpoint_defaults():
    return {
        "name": "",
        "enabled": "1",
        "groups": [],
        "entries": [],
        "poll_interval": str(int(DEFAULT_POLL_INTERVAL)),
        "poll_interval_watch": str(int(DEFAULT_WATCH_POLL_INTERVAL)),
    }


def endpoint_draft_from_row(row):
    return {
        "name": str(row.get("name") or ""),
        "enabled": "1" if int(row.get("enabled") or 0) else "0",
        "groups": [token for token in normalize_groups_value(row.get("groups") or "").split(".") if token],
        "entries": parse_entries(row.get("entries_json")),
        "poll_interval": str(clamp_poll_interval(row.get("poll_interval"), DEFAULT_POLL_INTERVAL)),
        "poll_interval_watch": str(clamp_poll_interval(row.get("poll_interval_watch"), DEFAULT_WATCH_POLL_INTERVAL)),
    }


def validate_draft(draft):
    if not str(draft.get("name") or "").strip():
        raise ValueError("Enter a name for this endpoint.")
    if not draft.get("groups"):
        raise ValueError("Choose at least one recipient, or select All Recipients.")
    if not draft.get("entries"):
        raise ValueError("Add at least one alert entry.")
    for item in draft.get("entries") or []:
        if not str(item.get("alert_key") or "").strip():
            raise ValueError("Choose an alert type for each entry, or remove empty entries.")
    normalized_entries = [normalize_entry(item) for item in draft.get("entries") or []]
    return {
        "name": str(draft.get("name") or "").strip(),
        "enabled": 1 if str(draft.get("enabled")) == "1" else 0,
        "groups": normalize_groups_value(draft.get("groups") or []),
        "entries_json": json.dumps(normalized_entries, separators=(",", ":"), ensure_ascii=True),
        "poll_interval": clamp_poll_interval(draft.get("poll_interval"), DEFAULT_POLL_INTERVAL),
        "poll_interval_watch": clamp_poll_interval(draft.get("poll_interval_watch"), DEFAULT_WATCH_POLL_INTERVAL),
    }


NWS_SERVICE_CONFIG_SCRIPT = """
<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet"/>
<style>
.sm-section{border:1px solid #e6e8eb;border-radius:6px;padding:0;display:block;overflow:hidden}
.sm-section>.sm-msg{padding:12px}
.sm-section-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;cursor:pointer;list-style:none;padding:12px;background:#f7f8fa}
.sm-section-head::-webkit-details-marker{display:none}
.sm-section-head::before{content:"\\25B6";font-size:.7em;color:#5f6368;transition:transform .15s ease;margin-right:2px}
details[open]>.sm-section-head::before{transform:rotate(90deg)}
.sm-section-title{font-weight:500;flex:1}
.sm-entries-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:4px}
.sm-entries-title{font-weight:600}
.sm-icon-btn{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:6px;border:1px solid #d0d3d6;background:#fff;cursor:pointer;color:#3c4043}
.sm-icon-btn.add{border-color:var(--ops-accent);color:var(--ops-accent)}
.sm-icon-btn.danger{border-color:#e5a3a3;color:#c62828}
.sm-icon-btn:hover{filter:brightness(0.96)}
.checkbox-row{display:flex;flex-direction:column;gap:6px}
.sm-msg{display:block}
.sm-editor-frame{width:100%;border:1px solid #e6e8eb;border-radius:6px;background:#fff;display:block;min-height:220px}
.sm-empty{color:#5f6368;font-size:.9em;margin:0}
@media(prefers-color-scheme:dark){.sm-section{border-color:#333}.sm-section-head{background:#1d1d1d}.sm-icon-btn{background:#171717;border-color:#333;color:#ddd}.sm-editor-frame{border-color:#333;background:#171717}.sm-empty{color:#aaa}.sm-section-head::before{color:#aaa}}
html.sm-modal-active,html.sm-modal-active body{background:transparent !important;overflow:hidden !important}
html.sm-modal-active body>*{visibility:hidden !important}
html.sm-modal-active .sm-editor-frame.sm-active{visibility:visible !important}
</style>
<script>
(function(){
  window.addEventListener('message',function(ev){
    var d=ev.data;
    if(!d)return;
    if(d.type==='sm-modal-overlay'){
      var frames2=document.querySelectorAll('.sm-editor-frame');
      for(var j=0;j<frames2.length;j++){
        if(frames2[j].contentWindow===ev.source){
          var f=frames2[j];
          if(d.open){
            if(f.getAttribute('data-sm-prev')===null)f.setAttribute('data-sm-prev',f.getAttribute('style')||'');
            document.documentElement.classList.add('sm-modal-active');
            f.classList.add('sm-active');
            f.style.position='fixed';f.style.top='0';f.style.left='0';f.style.width='100vw';f.style.height='100vh';
            f.style.right='auto';f.style.bottom='auto';f.style.margin='0';f.style.border='0';f.style.borderRadius='0';
            f.style.background='transparent';f.style.minHeight='0';f.style.zIndex='2147483000';
          }else{
            f.setAttribute('style',f.getAttribute('data-sm-prev')||'');f.removeAttribute('data-sm-prev');
            f.classList.remove('sm-active');document.documentElement.classList.remove('sm-modal-active');
          }
          break;
        }
      }
      try{window.parent.postMessage({type:'sm-modal-overlay',open:d.open},'*')}catch(e){}
      return;
    }
    if(d.type!=='ops-frame-height')return;
    var frames=document.querySelectorAll('.sm-editor-frame');
    for(var i=0;i<frames.length;i++){
      if(frames[i].contentWindow===ev.source){
        if(frames[i].classList.contains('sm-active'))break;
        frames[i].style.height=(Number(d.height)+4)+'px';
        break;
      }
    }
  });
  var form=document.getElementById('nwsServiceForm'),submitting=false;
  function flushEditors(done){
    var frames=Array.prototype.slice.call(document.querySelectorAll('.sm-editor-frame')),i=0;
    function next(){
      if(i>=frames.length){done();return}
      var f=frames[i++],acked=false;
      function settle(){if(acked)return;acked=true;clearTimeout(timer);window.removeEventListener('message',onMsg);next()}
      function onMsg(ev){if(!ev.data||ev.data.type!=='sm-flushed')return;if(ev.source&&f.contentWindow&&ev.source!==f.contentWindow)return;settle()}
      var timer=setTimeout(settle,2500);
      window.addEventListener('message',onMsg);
      try{f.contentWindow.postMessage({type:'sm-flush'},window.location.origin)}catch(e){settle()}
    }
    next();
  }
  document.querySelectorAll('[data-nws-action]').forEach(function(button){button.addEventListener('click',function(){document.getElementById('nwsAction').value=button.getAttribute('data-nws-action')||'save'})});
  function toggleRecipients(){var all=document.getElementById('send_all');if(!all)return;document.querySelectorAll('.group-checkbox').forEach(function(cb){cb.disabled=all.checked;if(all.checked)cb.checked=false})}
  var sendAll=document.getElementById('send_all');if(sendAll)sendAll.addEventListener('change',toggleRecipients);
  toggleRecipients();
  if(form)form.addEventListener('submit',function(ev){if(submitting)return;ev.preventDefault();flushEditors(function(){submitting=true;form.submit()})});
})();
</script>
"""


def collect_service_config(draft, form):
    draft["name"] = str(form.get("name") or "").strip()
    draft["enabled"] = "1" if form.get("enabled") else "0"
    if form.get("poll_interval") is not None:
        draft["poll_interval"] = str(clamp_poll_interval(form.get("poll_interval"), DEFAULT_POLL_INTERVAL))
    if form.get("poll_interval_watch") is not None:
        draft["poll_interval_watch"] = str(clamp_poll_interval(form.get("poll_interval_watch"), DEFAULT_WATCH_POLL_INTERVAL))
    if form.get("send_all"):
        draft["groups"] = ["0"]
    else:
        draft["groups"] = [str(item).strip() for item in form.getlist("groups[]") if str(item).strip()]
    return draft


def zone_kind_label(kind):
    token = str(kind or "").strip().lower()
    if token == "county":
        return "County"
    if token == "marine":
        return "Marine Zone"
    return str(kind or "").strip().title()


def zone_name_lookup():
    catalog = load_zone_catalog_safe()
    lookup = {}
    for items in (catalog.get("grouped") or {}).values():
        for zone in items:
            code = str(zone.get("code") or "").strip().upper()
            if code:
                lookup[code] = str(zone.get("name") or code)
    return lookup


def entry_location_label(entry, lookup=None):
    if lookup is None:
        lookup = zone_name_lookup()
    names = []
    for code in entry.get("zones") or []:
        key = str(code or "").strip().upper()
        if not key:
            continue
        names.append(lookup.get(key, key))
    joined = ", ".join(names)
    if len(joined) > 70:
        joined = joined[:67].rstrip(", ") + "…"
    return joined


def service_zone_checkboxes(selected):
    wanted = {str(item).upper() for item in selected or []}
    catalog = load_zone_catalog_safe()
    grouped = catalog.get("grouped") or {}
    blocks = []
    for code in sorted(grouped, key=lambda value: state_label(value)):
        rows = []
        for zone in grouped[code]:
            zcode = str(zone.get("code") or "")
            kind = zone_kind_label(zone.get("kind"))
            search = f'{zone.get("name") or ""} {zcode} {kind}'.lower()
            checked = " checked" if zcode.upper() in wanted else ""
            rows.append(
                f'<label class="md-checkbox-container zone-row" data-search="{h(search)}">'
                f'<input type="checkbox" name="zones[]" value="{h(zcode)}"{checked}>'
                f'<span class="md-checkmark"></span>'
                f'<span class="md-checkbox-text">{h(zone.get("name"))} {h(kind)} ({h(zcode)})</span></label>'
            )
        blocks.append(
            f'<div class="zone-group" data-state="{h(state_label(code).lower())}">'
            f'<div class="zone-group-head">{h(state_label(code))}</div>{"".join(rows)}</div>'
        )
    return "".join(blocks) or '<span class="help-text">No locations available.</span>'


def collect_service_entry(form):
    from srv.web.pages.messages.form_common import message_expiration_from_form, vendor_specific_from_form

    if form.get("expiration_when_alert"):
        tokens = []
        if form.get("expiration_manual"):
            tokens.append("manual")
        tokens.append("alertm")
        if form.get("expiration_when_message"):
            if form.get("expiration_any_message"):
                tokens.append("msg=*")
            else:
                seen = []
                for item in form.getlist("expiration_message_ids[]"):
                    token = str(item or "").strip()
                    if token and token not in seen:
                        seen.append(token)
                if seen:
                    tokens.append("msg=" + ".".join(seen))
        expires = "|".join(tokens)
    else:
        expires = message_expiration_from_form(form)
    return {
        "id": str(form.get("entry_id") or "").strip() or uuid.uuid4().hex,
        "alert_key": str(form.get("alert_key") or "").strip(),
        "zones": [str(item).strip().upper() for item in form.getlist("zones[]") if str(item).strip()],
        "shortmessage": str(form.get("shortmessage") or ""),
        "longmessage": str(form.get("longmessage") or ""),
        "priority": str(form.get("priority") or "Normal"),
        "color": str(form.get("color") or "").strip().lstrip("#").upper(),
        "icon": str(form.get("icon") or "").strip(),
        "audio": [str(item).strip() for item in form.getlist("audio_files[]") if str(item).strip()],
        "expires": expires,
        "vendor_specific": vendor_specific_from_form(form, ""),
    }


def service_entry_editor_html(entry, token, error=""):
    from srv.web.pages.messages.form_common import (
        MESSAGE_FORM_SCRIPT,
        MESSAGE_FORM_STYLE,
        audio_transfer_html,
        message_expiration_field_html,
        message_icon_field_html,
        message_variable_field_html,
        message_variable_guide_html,
        vendor_specific_editor_html,
    )

    try:
        from srv.web import app as webapp
        available_audio = webapp.audio_files()
    except Exception:
        available_audio = []
    try:
        expiration_messages = query_all(
            "SELECT `messageid`, `name` FROM `messages` ORDER BY `name` ASC, `messageid` ASC"
        )
    except Exception:
        expiration_messages = []

    has_alert = bool(entry.get("alert_key"))
    alert_select = f'<option value="" disabled{"" if has_alert else " selected"}>Select an event…</option>' + "".join(
        f'<option value="{h(spec["id"])}"{" selected" if entry.get("alert_key") == spec["id"] else ""}>'
        f'{h(spec["event"])}</option>'
        for spec in alert_options()
    )
    priority_options = "".join(
        f'<option value="{h(value)}"{" selected" if entry.get("priority") == value else ""}>{h(value)}</option>'
        for value in ("Low", "Normal", "High", "Emergency")
    )
    message_fields = message_variable_field_html(
        "shortmessage",
        "Short Message",
        f'<input type="text" name="shortmessage" id="shortmessage" class="form-control" value="{h(entry.get("shortmessage"))}">',
        "",
    ) + message_variable_field_html(
        "longmessage",
        "Long Message",
        f'<textarea name="longmessage" id="longmessage" class="form-control textarea-long" rows="7" wrap="soft">{h(entry.get("longmessage"))}</textarea>',
        "",
    )
    audio_field = audio_transfer_html(available_audio, list(entry.get("audio") or []))
    entry_expires = str(entry.get("expires") or "alert")
    expires_tokens = [token for token in entry_expires.split("|") if token]
    when_alert = ("alert" in expires_tokens) or ("alertm" in expires_tokens)
    remainder = [token for token in expires_tokens if token not in ("alert", "alertm")]
    sm_expires_value = "|".join(remainder) if remainder else "manual"
    sm_manual_explicit = "manual" in remainder
    expiration_field = message_expiration_field_html(expiration_messages, sm_expires_value, include_on_up=False)
    when_alert_checked = " checked" if when_alert else ""
    manual_intent_js = "true" if sm_manual_explicit else "false"
    try:
        vendor_field = vendor_specific_editor_html(str(entry.get("vendor_specific") or ""), context={"mode": "message_custom"})
    except Exception:
        vendor_field = ""
    color = str(entry.get("color") or "")
    color_default = "#" + color if re.fullmatch(r"[A-Fa-f0-9]{6}", color) else "#000000"
    icon_field = message_icon_field_html(str(entry.get("icon") or ""))
    error_html = f'<div class="error">{h(error)}</div>' if error else ""
    variable_choices = "".join(
        f'<button type="button" class="message-variable-choice" onclick="insertVariableSnippet(\'${{{h(key)}}}\')">{h(label)}</button>'
        for key, label in VARIABLE_LABELS.items()
    )
    defaults_json = json.dumps(
        {key: default_entry_for_alert(key) for key in ALERT_ORDER}, separators=(",", ":")
    )
    audio_files_json = json.dumps(list(available_audio), separators=(",", ":"))
    body_style = "" if has_alert else "display:none"
    return f"""<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet"/>
<style>{MESSAGE_FORM_STYLE}
body,html{{overflow:visible;height:auto}}
.zone-list{{max-height:280px;overflow:auto;border:1px solid #DDD;border-radius:6px;padding:8px}}
.zone-group-head{{font-weight:600;margin:10px 0 4px;font-size:.85em;text-transform:uppercase;letter-spacing:.03em;color:#5f6368}}
.zone-row{{display:flex;align-items:center;gap:10px}}
.nws-zone-search{{margin-bottom:8px}}
@media(prefers-color-scheme:dark){{.zone-list{{border-color:#333}}.zone-group-head{{color:#aaa}}}}
html.sm-modal-active,html.sm-modal-active body{{background:transparent !important;overflow:hidden !important}}
html.sm-modal-active body>*{{visibility:hidden !important}}
html.sm-modal-active .message-variable-modal.open,html.sm-modal-active .message-variable-modal-backdrop.open,html.sm-modal-active .message-icon-picker-modal.open,html.sm-modal-active .message-icon-picker-backdrop.open,html.sm-modal-active .audio-block-modal.open,html.sm-modal-active .audio-block-modal-backdrop.open{{visibility:visible !important}}
</style>
<div style="padding:16px;box-sizing:border-box"><div class="info-card">{error_html}
<form method="post" id="nwsEntryForm">
<input type="hidden" name="token" value="{h(token)}"><input type="hidden" name="entry_id" value="{h(entry.get("id"))}">
<div class="form-group"><label class="main-label" for="entryAlertKey">Event</label><select class="form-control" name="alert_key" id="entryAlertKey" onchange="applyNwsDefaults()">{alert_select}</select></div>
<div id="entryBody" style="{body_style}">
<div class="form-group"><label class="main-label" for="zoneSearch">Locations</label><input type="text" id="zoneSearch" class="form-control nws-zone-search" placeholder="Search locations…" oninput="filterZones()"><div class="zone-list" id="entryZones">{service_zone_checkboxes(entry.get("zones"))}</div></div>
<div id="audio-fields" class="form-group"><label class="main-label">Audio</label>{audio_field}</div>
{message_fields}
{icon_field}
<div class="form-group"><label class="main-label">Color</label><div class="color-picker-container"><input type="color" id="colorPicker" value="{h(color_default)}" class="color-picker-input"><input type="text" name="color" id="colorHex" class="form-control" style="width:150px" placeholder="000000" maxlength="6" value="{h(color)}"></div></div>
<div id="smExpiration">{expiration_field}<label class="md-checkbox-container" id="nwsExpireLabel"><input type="checkbox" id="nwsExpiresAlert" name="expiration_when_alert" value="1"{when_alert_checked}><span class="md-checkmark"></span><span class="message-expiration-text"><span class="message-expiration-title">On event expiration</span></span></label></div>
<div class="form-group"><label class="main-label" for="entryPriority">Priority</label><select class="form-control" name="priority" id="entryPriority">{priority_options}</select></div>
{vendor_field}
</div>
</form></div>{message_variable_guide_html(variable_choices)}</div>
<script>{MESSAGE_FORM_SCRIPT}
var nwsDefaults={defaults_json};
var nwsAudioFiles={audio_files_json};
function nwsPickVoice(){{var map=(typeof audioTtsVoiceData==='function')?audioTtsVoiceData():{{}};var vals=Object.keys(map).map(function(k){{return map[k]}});var i,p;for(i=0;i<vals.length;i++){{if(String(vals[i].engine||'').toLowerCase()!=='google')return vals[i]}}var prefs=['en','en-us','en-gb'];for(p=0;p<prefs.length;p++){{for(i=0;i<vals.length;i++){{if(String(vals[i].engine||'').toLowerCase()==='google'&&String(vals[i].voice||'').toLowerCase()===prefs[p])return vals[i]}}}}for(i=0;i<vals.length;i++){{if(String(vals[i].engine||'').toLowerCase()==='google')return vals[i]}}return null}}
function nwsMakeTts(voice,text){{var payload={{engine:voice.engine,voice:voice.voice,voice_label:voice.voice_label||voice.display_name||voice.voice,text:text}};if(voice.engine==='piper'){{payload.model_path=voice.model_path||'';payload.config_path=voice.config_path||'';if(voice.sample_rate)payload.sample_rate=Number(voice.sample_rate)}}return {{kind:'tts',value:encodeAudioTtsToken(payload),text:text,engine:voice.engine,voice:voice.voice,voiceLabel:payload.voice_label,voiceId:voice.id}}}}
function nwsDefaultAudio(key){{var list=(typeof audioBlockList==='function')?audioBlockList():null;if(!list)return;Array.prototype.slice.call(list.querySelectorAll('.audio-block-item')).forEach(function(el){{el.remove()}});var base=nwsDefaults[key]||{{}};var priority=base.priority||'Normal';var voice=nwsPickVoice();var tts='National Weather Service has issued a ${{alertname}} for ${{locations}}';if(key==='tornado_warning'){{tts='SHELTER! For a tornado. Go to your tornado shelter. National Weather Service has issued a Tornado Warning for ${{locations}}'}}var tone=null,repeat=1;if(priority==='High'){{tone='OPS-900HZ-SlowPulse.wav';repeat=3}}else if(priority==='Emergency'){{tone='OPS-400HZ-MedPulse.wav';repeat=5}}else{{repeat=1}}if(tone&&nwsAudioFiles.indexOf(tone)>=0){{appendAudioBlockItem(createAudioBlockElement({{kind:'file',title:tone,value:tone}}))}}if(voice){{for(var r=0;r<repeat;r++){{appendAudioBlockItem(createAudioBlockElement(nwsMakeTts(voice,tts)))}}}}if(typeof updateAudioBlockEmptyState==='function')updateAudioBlockEmptyState()}}
function applyNwsDefaults(){{var key=document.getElementById('entryAlertKey').value;var body=document.getElementById('entryBody');if(body)body.style.display=key?'':'none';var base=nwsDefaults[key];if(!base)return;document.getElementById('shortmessage').value=base.shortmessage;document.getElementById('longmessage').value=base.longmessage;document.getElementById('entryPriority').value=base.priority;document.getElementById('colorHex').value=base.color;document.getElementById('colorPicker').value='#'+base.color;nwsDefaultAudio(key)}}
function filterZones(){{var q=(document.getElementById('zoneSearch').value||'').toLowerCase();var groups=document.querySelectorAll('#entryZones .zone-group');Array.prototype.forEach.call(groups,function(g){{var st=g.getAttribute('data-state')||'';var any=false;Array.prototype.forEach.call(g.querySelectorAll('.zone-row'),function(r){{var s=r.getAttribute('data-search')||'';var m=(st.indexOf(q)>=0)||(s.indexOf(q)>=0);r.style.display=m?'':'none';if(m)any=true}});g.style.display=any?'':'none'}})}}
function syncNwsExpiration(){{var box=document.getElementById('nwsExpiresAlert');var immediate=document.getElementById('messageExpirationImmediate');var manual=document.getElementById('messageExpirationManual');var after=document.getElementById('messageExpirationAfterEnabled');var afterMin=document.getElementById('messageExpirationAfterMinutes');if(!box)return;if(immediate&&immediate.checked){{box.checked=false;box.disabled=true}}else{{box.disabled=false}}var on=box.checked;if(after){{if(on)after.checked=false;after.disabled=on||(immediate&&immediate.checked)}}if(afterMin){{afterMin.disabled=on||(after&&after.disabled)}}if(on&&manual){{manual.checked=nwsManualIntent}}}}
function nwsExpireChanged(){{if(typeof syncMessageExpiration==='function'){{try{{syncMessageExpiration()}}catch(e){{}}}}syncNwsExpiration()}}
var nwsManualIntent={manual_intent_js};
(function(){{var label=document.getElementById('nwsExpireLabel');var manual=document.getElementById('messageExpirationManual');if(label&&manual){{var mc=manual.closest('.md-checkbox-container');if(mc&&mc.parentNode)mc.parentNode.insertBefore(label,mc.nextSibling)}}if(manual)manual.addEventListener('change',function(){{nwsManualIntent=manual.checked}});var box=document.getElementById('nwsExpiresAlert');if(box)box.addEventListener('change',nwsExpireChanged);var sm=document.getElementById('smExpiration');if(sm)sm.addEventListener('change',function(){{syncNwsExpiration()}})}})();
if(typeof syncMessageExpiration==='function'){{try{{syncMessageExpiration()}}catch(e){{}}}}
syncNwsExpiration();
(function(){{
  var form=document.getElementById('nwsEntryForm');
  if(!form)return;
  var pending=null,inflight=false,again=false;
  function saveNow(){{var fd=new FormData(form);fd.set('action','autosave_entry');return fetch(window.location.href,{{method:'POST',body:fd,credentials:'same-origin'}})}}
  function runSave(){{if(inflight){{again=true;return}}inflight=true;saveNow().then(function(){{inflight=false;if(again){{again=false;runSave()}}}},function(){{inflight=false}})}}
  function scheduleSave(){{if(pending)clearTimeout(pending);pending=setTimeout(function(){{pending=null;runSave()}},600)}}
  form.addEventListener('input',scheduleSave);
  form.addEventListener('change',scheduleSave);
  var list=(typeof audioBlockList==='function')?audioBlockList():document.getElementById('audioBlockList');
  if(list){{new MutationObserver(scheduleSave).observe(list,{{childList:true,subtree:true,attributes:true}})}}
  window.addEventListener('message',function(ev){{if(!ev.data||ev.data.type!=='sm-flush')return;function ack(){{if(window.parent&&window.parent!==window)window.parent.postMessage({{type:'sm-flushed'}},window.location.origin)}}if(pending){{clearTimeout(pending);pending=null}}saveNow().then(ack,ack)}});
}})();
(function(){{
  var query='.message-variable-modal.open,.message-variable-modal-backdrop.open,.message-icon-picker-modal.open,.message-icon-picker-backdrop.open,.audio-block-modal.open,.audio-block-modal-backdrop.open',lastOpen=false;
  function check(){{var open=!!document.querySelector(query);if(open===lastOpen)return;lastOpen=open;document.documentElement.classList.toggle('sm-modal-active',open);try{{window.parent.postMessage({{type:'sm-modal-overlay',open:open}},'*')}}catch(e){{}}}}
  var observer=new MutationObserver(check);observer.observe(document.documentElement,{{attributes:true,subtree:true,attributeFilter:['class']}});
}})();
</script>"""


def service_form_html(mode, endpoint_id, token, draft, user, error=""):
    groups = group_rows_for_user(user)
    selected_groups = {str(item) for item in draft.get("groups") or []}
    send_all = "0" in selected_groups
    group_checkboxes = "".join(
        f'<label class="md-checkbox-container"><input type="checkbox" name="groups[]" value="{h(row.get("id"))}" '
        f'class="group-checkbox"{" disabled" if send_all else ""}'
        f'{" checked" if str(row.get("id")) in selected_groups and not send_all else ""}>'
        f'<span class="md-checkmark"></span><span class="md-checkbox-text">{h(row.get("name") or row.get("id"))}</span></label>'
        for row in groups
    ) or '<span class="hint">No groups configured.</span>'
    if mode == "edit":
        editor_base = f"/admin/endpoint-action-frame?module=nws&amp;action=edit&amp;id={h(endpoint_id)}&amp;view=editor"
    else:
        editor_base = f"/admin/endpoint-form-frame?module=nws&amp;type=endpoint&amp;view=editor&amp;token={h(token)}"
    lookup = zone_name_lookup()
    sections = []
    for entry in draft.get("entries") or []:
        spec = ALERT_SPECS.get(entry.get("alert_key")) or {}
        entry_id = str(entry.get("id") or "")
        event = str(spec.get("event") or "")
        locations = entry_location_label(entry, lookup)
        if event and locations:
            title = f"{event} for {locations}"
        elif event:
            title = event
        else:
            title = "New alert entry"
        sections.append(
            f'<details class="sm-section"><summary class="sm-section-head">'
            f'<span class="sm-section-title">{h(title)}</span>'
            f'<button type="submit" class="sm-icon-btn danger" title="Delete entry" data-nws-action="delete_entry:{h(entry_id)}"><i class="fa-solid fa-trash"></i></button>'
            f'</summary>'
            f'<div class="sm-msg"><iframe class="sm-editor-frame" title="{h(title)}" src="{editor_base}&amp;entry={h(entry_id)}" scrolling="no"></iframe></div></details>'
        )
    entries_html = "".join(sections) or '<p class="sm-empty">No alert entries added yet.</p>'
    error_html = f'<div class="error">{h(error)}</div>' if error else ""
    endpoint_hidden = f'<input type="hidden" name="endpoint_id" value="{h(endpoint_id)}">' if endpoint_id else ""
    submit_label = "Save NWS Alert Endpoint" if mode == "edit" else "Add NWS Alert Endpoint"
    return f"""{error_html}<form method="post" class="grid form-surface" id="nwsServiceForm">
<input type="hidden" name="token" value="{h(token)}"><input type="hidden" name="action" id="nwsAction" value="save">{endpoint_hidden}
<div class="row"><label>Name <span style="color:#C62828">*</span></label><input class="control" name="name" value="{h(draft.get("name"))}" required></div>
<label class="md-checkbox-container"><input type="checkbox" name="enabled" value="1"{" checked" if str(draft.get("enabled")) == "1" else ""}><span class="md-checkmark"></span><span class="md-checkbox-text">Enabled</span></label>
<div class="row"><label>Polling Frequency (seconds)</label><input class="control" type="number" name="poll_interval" min="5" max="86400" value="{h(draft.get("poll_interval") or int(DEFAULT_POLL_INTERVAL))}"></div>
<div class="row"><label>Polling Frequency during severe weather watches (seconds)</label><input class="control" type="number" name="poll_interval_watch" min="5" max="86400" value="{h(draft.get("poll_interval_watch") or int(DEFAULT_WATCH_POLL_INTERVAL))}"></div>
<div class="row"><label>Recipients</label><div class="checkbox-row"><label class="md-checkbox-container"><input type="checkbox" name="send_all" id="send_all" value="1"{" checked" if send_all else ""}><span class="md-checkmark"></span><span class="md-checkbox-text" style="font-weight:bold;color:var(--ops-accent)">All Recipients</span></label>{group_checkboxes}</div></div>
<div class="sm-entries-head"><span class="sm-entries-title">Alert Events</span><button class="sm-icon-btn add" type="submit" data-nws-action="add_entry" title="Add event"><i class="fa-solid fa-plus"></i></button></div>
{entries_html}
<button class="button" type="submit" data-nws-action="save">{h(submit_label)}</button>
</form>{NWS_SERVICE_CONFIG_SCRIPT}"""


def zones_payload():
    catalog = load_zone_catalog_safe()
    grouped = {}
    for state_code, items in (catalog.get("grouped") or {}).items():
        grouped[state_code] = {
            "label": state_label(state_code),
            "zones": items,
        }
    return {"ok": True, "zone_count": len(catalog.get("zones") or []), "grouped": grouped, "updated_at": catalog.get("updated_at") or ""}


def _handle(mode, endpoint_id, request, page, user):
    if str(request.args.get("api") or "").strip().lower() == "zones":
        return jsonify(zones_payload())
    if str(request.args.get("api") or "").strip().lower() == "alert-types":
        try:
            official = official_alert_type_names()
        except Exception:
            official = []
        return jsonify({"ok": True, "official": official, "configured": [ALERT_SPECS[key]["event"] for key in ALERT_ORDER]})
    row = None
    row_id = None
    if mode == "edit":
        prefix, _, row_id = str(endpoint_id or "").partition("-")
        if prefix != "nws" or not row_id.isdigit():
            return page("Endpoint Action", "<h1>Invalid endpoint action</h1>", "endpoints", user, status=400)
        row = endpoint_row(row_id)
        if not row:
            return page("Endpoint Action", "<h1>Endpoint not found</h1>", "endpoints", user, status=404)
        token = f"edit-nws-{row_id}"
    view = str(request.args.get("view") or "").strip().lower()
    entry_id = str(request.args.get("entry") or request.form.get("entry_id") or "").strip()
    if request.method == "GET":
        if mode == "new":
            token = str(request.args.get("token") or "").strip() or uuid.uuid4().hex
        if view == "editor":
            draft = load_draft(token)
            if draft is None:
                draft = endpoint_draft_from_row(row) if row else endpoint_defaults()
                save_draft(token, draft)
            entry = next((item for item in draft.get("entries") or [] if str(item.get("id")) == entry_id), None)
            if entry is None:
                return page("NWS Alerts", "<p>Alert entry not found.</p>", "endpoints", user, status=404)
            return page("NWS Alerts", service_entry_editor_html(entry, token), "endpoints", user)
        draft = endpoint_draft_from_row(row) if row else endpoint_defaults()
        save_draft(token, draft)
        body = service_form_html(mode, endpoint_id, token, draft, user)
        return page("NWS Alerts", endpoints.sip_form_frame(body), "endpoints", user)
    form = request.form
    if mode == "new":
        token = str(form.get("token") or "").strip() or uuid.uuid4().hex
    draft = load_draft(token)
    if draft is None:
        draft = endpoint_draft_from_row(row) if row else endpoint_defaults()
    action = str(form.get("action") or "save").strip()
    if action == "autosave_entry":
        updated = collect_service_entry(form)
        entries = list(draft.get("entries") or [])
        index = next((i for i, item in enumerate(entries) if str(item.get("id")) == str(updated.get("id"))), -1)
        if index >= 0:
            entries[index] = updated
        else:
            entries.append(updated)
        draft["entries"] = entries
        save_draft(token, draft)
        return page("NWS Alerts", "ok", "endpoints", user)
    collect_service_config(draft, form)
    if action == "add_entry":
        draft.setdefault("entries", []).append({
            "id": uuid.uuid4().hex,
            "alert_key": "",
            "zones": [],
            "shortmessage": "",
            "longmessage": "",
            "priority": "Normal",
            "color": "",
            "icon": "",
            "audio": [],
            "expires": "alert",
            "vendor_specific": "",
        })
        save_draft(token, draft)
        body = service_form_html(mode, endpoint_id, token, draft, user)
        return page("NWS Alerts", endpoints.sip_form_frame(body), "endpoints", user)
    if action.startswith("delete_entry:"):
        remove_id = action.partition(":")[2]
        draft["entries"] = [item for item in draft.get("entries") or [] if str(item.get("id")) != remove_id]
        save_draft(token, draft)
        body = service_form_html(mode, endpoint_id, token, draft, user)
        return page("NWS Alerts", endpoints.sip_form_frame(body), "endpoints", user)
    try:
        values = validate_draft(draft)
    except ValueError as exc:
        save_draft(token, draft)
        body = service_form_html(mode, endpoint_id, token, draft, user, str(exc))
        return page("NWS Alerts", endpoints.sip_form_frame(body), "endpoints", user)
    if mode == "edit":
        ensure_endpoint_columns()
        execute(
            f"UPDATE `{ENDPOINT_TABLE}` SET `name`=%s, `enabled`=%s, `groups`=%s, `entries_json`=%s, `poll_interval`=%s, `poll_interval_watch`=%s WHERE `id`=%s",
            (values["name"], values["enabled"], values["groups"], values["entries_json"], values["poll_interval"], values["poll_interval_watch"], row_id),
        )
    else:
        ensure_endpoint_columns()
        execute(
            f"INSERT INTO `{ENDPOINT_TABLE}` (`name`, `enabled`, `groups`, `entries_json`, `poll_interval`, `poll_interval_watch`) VALUES (%s,%s,%s,%s,%s,%s)",
            (values["name"], values["enabled"], values["groups"], values["entries_json"], values["poll_interval"], values["poll_interval_watch"]),
        )
    delete_draft(token)
    return page("Endpoint Saved", "<script>window.top.location.href='/admin/manage-endpoints'</script><p>Endpoint saved.</p>", "endpoints", user)


def render_form(form_type, request, conn_factory, page, user):
    if form_type not in forms():
        return page("Endpoint Form", "<h1>Endpoint form not found</h1>", "endpoints", user, status=404)
    return _handle("new", None, request, page, user)


def render_action(action, endpoint_id, request, conn_factory, page, user):
    prefix, _, row_id = str(endpoint_id or "").partition("-")
    if prefix != "nws" or not row_id.isdigit():
        return page("Endpoint Action", "<h1>Invalid endpoint action</h1>", "endpoints", user, status=400)
    row = endpoint_row(row_id)
    if not row:
        return page("Endpoint Action", "<h1>Endpoint not found</h1>", "endpoints", user, status=404)
    if action == "delete":
        if request.method == "POST":
            execute(f"DELETE FROM `{ACTIVE_TABLE}` WHERE `endpoint_id`=%s", (row_id,))
            execute(f"DELETE FROM `{ENDPOINT_TABLE}` WHERE `id`=%s", (row_id,))
            delete_draft(f"edit-nws-{row_id}")
            return page("Endpoint Saved", "<script>window.top.location.href='/admin/manage-endpoints'</script><p>Endpoint deleted.</p>", "endpoints", user)
        body = (
            '<form method="post" class="grid surface">'
            f'<p class="meta">Delete {h(row.get("name") or endpoint_id)}?</p>'
            '<button class="danger" type="submit">Delete Endpoint</button></form>'
        )
        return page("Endpoint Action", endpoints.sip_form_frame(body), "endpoints", user)
    if action != "edit":
        return page("Endpoint Action", "<h1>Invalid endpoint action</h1>", "endpoints", user, status=400)
    return _handle("edit", endpoint_id, request, page, user)


def render_settings(request, conn_factory, page, user):
    note = "This module uses the official NWS API at api.weather.gov and caches county and marine zone metadata locally."
    return page("NWS Alerts", endpoints.sip_form_frame(f"<p>{h(note)}</p>"), "endpoints", user)
