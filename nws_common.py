import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


ROOT_DIR = Path(__file__).resolve().parent
CACHE_DIR = ROOT_DIR / ".cache"
ZONE_CACHE_PATH = CACHE_DIR / "zones.json"
USER_AGENT = os.getenv("NWS_USER_AGENT", "(OpenPagingServer-NWS-Module, support@openpagingserver.local)")
API_ROOT = "https://api.weather.gov"
COUNTY_ZONE_URL = f"{API_ROOT}/zones?type=county"
MARINE_ZONE_URL = f"{API_ROOT}/zones?type=marine"
ACTIVE_ALERTS_URL = f"{API_ROOT}/alerts/active"
ALERT_TYPES_URL = f"{API_ROOT}/alerts/types"
ZONE_CACHE_MAX_AGE = 24 * 60 * 60
ACTIVE_ALERT_POLL_INTERVAL = 60
DEFAULT_POLL_INTERVAL = 180
DEFAULT_WATCH_POLL_INTERVAL = 60

STATE_NAMES = {
    "AK": "Alaska",
    "AL": "Alabama",
    "AR": "Arkansas",
    "AS": "American Samoa",
    "AZ": "Arizona",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DC": "District of Columbia",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "GM": "Gulf of America",
    "GU": "Guam",
    "HI": "Hawaii",
    "IA": "Iowa",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "LC": "Lake St. Clair",
    "LE": "Lake Erie",
    "LH": "Lake Huron",
    "LM": "Lake Michigan",
    "LO": "Lake Ontario",
    "LS": "Lake Superior",
    "MA": "Massachusetts",
    "MD": "Maryland",
    "ME": "Maine",
    "MH": "Marshall Islands",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MO": "Missouri",
    "MP": "Northern Mariana Islands",
    "MS": "Mississippi",
    "MT": "Montana",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "NE": "Nebraska",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NV": "Nevada",
    "NY": "New York",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "PH": "Central Pacific",
    "PK": "North Pacific Near Alaska",
    "PM": "Western Pacific",
    "PR": "Puerto Rico",
    "PZ": "Eastern Pacific",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UM": "U.S. Minor Outlying Islands",
    "UT": "Utah",
    "VA": "Virginia",
    "VI": "U.S. Virgin Islands",
    "VT": "Vermont",
    "WA": "Washington",
    "WI": "Wisconsin",
    "WV": "West Virginia",
    "WY": "Wyoming",
    "AM": "Atlantic Marine",
    "AN": "Atlantic Marine",
    "GMZ": "Gulf Marine",
}

_RAW_ALERTS = [
    ("special_weather_statement", "Special Weather Statement", "Low", "FFE4B5"),
    ("hydrologic_outlook", "Hydrologic Outlook", "Low", "98FB98"),
    ("air_quality_alert", "Air Quality Alert", "Low", "808080"),
    ("air_stagnation_advisory", "Air Stagnation Advisory", "Low", "808080"),
    ("frost_advisory", "Frost Advisory", "Low", "6495ED"),
    ("small_craft_advisory", "Small Craft Advisory", "Low", "D8BFD8"),
    ("beach_hazards_statement", "Beach Hazards Statement", "Low", "40E0D0"),
    ("rip_current_statement", "Rip Current Statement", "Low", "40E0D0"),
    ("marine_weather_statement", "Marine Weather Statement", "Low", "BDB76B"),
    ("winter_storm_watch", "Winter Storm Watch", "Normal", "0000CD"),
    ("high_wind_watch", "High Wind Watch", "Normal", "B8860B"),
    ("excessive_heat_watch", "Excessive Heat Watch", "Normal", "800000"),
    ("extreme_cold_watch", "Extreme Cold Watch", "Normal", "4169E1"),
    ("hurricane_watch", "Hurricane Watch", "Normal", "FF00FF"),
    ("tropical_storm_watch", "Tropical Storm Watch", "Normal", "F08080"),
    ("storm_surge_watch", "Storm Surge Watch", "Normal", "DB7FF7"),
    ("fire_weather_watch", "Fire Weather Watch", "Normal", "FFDEAD"),
    ("winter_weather_advisory", "Winter Weather Advisory", "Normal", "7B68EE"),
    ("wind_advisory", "Wind Advisory", "Normal", "D2B48C"),
    ("heat_advisory", "Heat Advisory", "Normal", "FF7F50"),
    ("cold_weather_advisory", "Cold Weather Advisory", "Normal", "AFEEEE"),
    ("dense_fog_advisory", "Dense Fog Advisory", "Normal", "708090"),
    ("dense_smoke_advisory", "Dense Smoke Advisory", "Normal", "F0E68C"),
    ("dust_advisory", "Dust Advisory", "Normal", "BDB76B"),
    ("flood_advisory", "Flood Advisory", "Normal", "00FF7F"),
    ("coastal_flood_advisory", "Coastal Flood Advisory", "Normal", "7CFC00"),
    ("lakeshore_flood_advisory", "Lakeshore Flood Advisory", "Normal", "7CFC00"),
    ("freezing_fog_advisory", "Freezing Fog Advisory", "Normal", "008080"),
    ("freezing_spray_advisory", "Freezing Spray Advisory", "Normal", "00BFFF"),
    ("high_surf_advisory", "High Surf Advisory", "Normal", "BA55D3"),
    ("gale_watch", "Gale Watch", "Normal", "FFB6C1"),
    ("storm_watch", "Storm Watch", "Normal", "FFE4B5"),
    ("hurricane_force_wind_watch", "Hurricane Force Wind Watch", "Normal", "9932CC"),
    ("small_craft_advisory_for_hazardous_seas", "Small Craft Advisory for Hazardous Seas", "Normal", "D8BFD8"),
    ("tornado_watch", "Tornado Watch", "High", "FFFF00"),
    ("severe_thunderstorm_watch", "Severe Thunderstorm Watch", "High", "DB7093"),
    ("flood_watch", "Flood Watch", "High", "2E8B57"),
    ("flash_flood_watch", "Flash Flood Watch", "High", "2E8B57"),
    ("severe_thunderstorm_warning", "Severe Thunderstorm Warning", "High", "FFA500"),
    ("flood_warning", "Flood Warning", "High", "00FF00"),
    ("areal_flood_warning", "Areal Flood Warning", "High", "00FF00"),
    ("coastal_flood_warning", "Coastal Flood Warning", "High", "228B22"),
    ("lakeshore_flood_warning", "Lakeshore Flood Warning", "High", "228B22"),
    ("winter_storm_warning", "Winter Storm Warning", "High", "FF69B4"),
    ("blizzard_warning", "Blizzard Warning", "High", "FF4500"),
    ("ice_storm_warning", "Ice Storm Warning", "High", "8B008B"),
    ("snow_squall_warning", "Snow Squall Warning", "High", "C71585"),
    ("high_wind_warning", "High Wind Warning", "High", "DAA520"),
    ("excessive_heat_warning", "Excessive Heat Warning", "High", "C71585"),
    ("extreme_cold_warning", "Extreme Cold Warning", "High", "0000FF"),
    ("freeze_warning", "Freeze Warning", "High", "483D8B"),
    ("red_flag_warning", "Red Flag Warning", "High", "FF1493"),
    ("tropical_storm_warning", "Tropical Storm Warning", "High", "B22222"),
    ("hurricane_warning", "Hurricane Warning", "High", "DC143C"),
    ("storm_surge_warning", "Storm Surge Warning", "High", "B524F7"),
    ("special_marine_warning", "Special Marine Warning", "High", "FFA500"),
    ("gale_warning", "Gale Warning", "High", "DDA0DD"),
    ("storm_warning", "Storm Warning", "High", "9400D3"),
    ("hurricane_force_wind_warning", "Hurricane Force Wind Warning", "High", "CD5C5C"),
    ("hazardous_seas_warning", "Hazardous Seas Warning", "High", "D8BFD8"),
    ("high_surf_warning", "High Surf Warning", "High", "228B22"),
    ("dust_storm_warning", "Dust Storm Warning", "High", "FFE4C4"),
    ("tornado_warning", "Tornado Warning", "Emergency", "FF0000"),
    ("flash_flood_warning", "Flash Flood Warning", "Emergency", "8B0000"),
    ("extreme_wind_warning", "Extreme Wind Warning", "Emergency", "FF8C00"),
    ("tsunami_warning", "Tsunami Warning", "Emergency", "FD6347"),
    ("avalanche_warning", "Avalanche Warning", "Emergency", "1E90FF"),
]

ALERT_SPECS = {
    key: {
        "id": key,
        "event": event,
        "priority": priority,
        "color": color,
        "match": "event",
    }
    for key, event, priority, color in _RAW_ALERTS
}

ALERT_ORDER = [item[0] for item in _RAW_ALERTS]
ALERT_EVENT_INDEX = {}
for spec in ALERT_SPECS.values():
    ALERT_EVENT_INDEX.setdefault(spec["event"].lower(), []).append(spec["id"])

WARNING_TO_WATCH_EVENT = {
    "severe_thunderstorm_warning": "severe thunderstorm watch",
    "tornado_warning": "tornado watch",
    "flash_flood_warning": "flash flood watch",
    "hurricane_warning": "hurricane watch",
    "tropical_storm_warning": "tropical storm watch",
    "storm_surge_warning": "storm surge watch",
    "high_wind_warning": "high wind watch",
    "winter_storm_warning": "winter storm watch",
    "red_flag_warning": "fire weather watch",
    "tsunami_warning": "tsunami watch",
}


def zone_code_name_lookup():
    catalog = load_zone_catalog_safe()
    lookup = {}
    for item in catalog.get("zones") or []:
        code = str(item.get("code") or "").strip().upper()
        if code:
            lookup[code] = str(item.get("name") or code).strip()
    return lookup


def endpoint_events_label(entries):
    events = []
    for entry in entries or []:
        spec = ALERT_SPECS.get(entry.get("alert_key"))
        if spec and spec["event"] not in events:
            events.append(spec["event"])
    return events


def endpoint_locations_label(entries, lookup=None):
    if lookup is None:
        lookup = zone_code_name_lookup()
    locations = []
    for entry in entries or []:
        for code in entry.get("zones") or []:
            name = lookup.get(str(code).strip().upper(), str(code).strip())
            if name and name not in locations:
                locations.append(name)
    return locations


def _summarize_label(items, limit=3):
    if not items:
        return ""
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f" +{len(items) - limit} more"


def endpoint_display_name(entries, lookup=None):
    events = _summarize_label(endpoint_events_label(entries)) or "Alerts"
    locations = _summarize_label(endpoint_locations_label(entries, lookup)) or "All Locations"
    return f"NWS {events} for {locations} (NWS Alerts)"


def clamp_poll_interval(value, default):
    try:
        seconds = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    if seconds < 5:
        return 5
    if seconds > 86400:
        return 86400
    return seconds


VARIABLE_LABELS = {
    "alertname": "Alert Name",
    "alertnamed": "Alert Name",
    "alerttext": "Alert Text",
    "locations": "Locations",
    "expiration": "Expiration",
}


def h(value):
    import html
    return html.escape("" if value is None else str(value), quote=True)


def requests_session():
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/geo+json, application/ld+json;q=0.9, application/json;q=0.8",
        }
    )
    return session


def ensure_cache_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def json_load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def json_dump(path, value):
    ensure_cache_dir()
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def now_utc():
    return datetime.now(timezone.utc)


def parse_iso8601(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except Exception:
        return None


def format_expiration(value):
    dt = parse_iso8601(value) if not isinstance(value, datetime) else value
    if dt is None:
        return ""
    return dt.astimezone().strftime("%m/%d/%Y %I:%M %p")


def slugify(value):
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return text or uuid.uuid4().hex


def normalize_groups_value(groups):
    if isinstance(groups, str):
        parts = re.split(r"[\s,\.]+", groups)
    else:
        parts = []
        for item in groups or []:
            parts.extend(re.split(r"[\s,\.]+", str(item or "")))
    clean = []
    for part in parts:
        token = str(part or "").strip()
        if token and token not in clean:
            clean.append(token)
    return ".".join(clean)


def normalize_entry(entry):
    raw = dict(entry or {})
    alert_key = str(raw.get("alert_key") or raw.get("alert") or "").strip()
    if alert_key not in ALERT_SPECS:
        raise ValueError("Choose a valid alert type.")
    zones = []
    for zone in raw.get("zones") or []:
        code = str(zone or "").strip().upper()
        if code and code not in zones:
            zones.append(code)
    if not zones:
        raise ValueError("Choose at least one county or marine zone.")
    shortmessage = str(raw.get("shortmessage") or "").strip()
    longmessage = str(raw.get("longmessage") or "").strip()
    if not shortmessage and not longmessage:
        raise ValueError("Add a short or long message.")
    priority = str(raw.get("priority") or ALERT_SPECS[alert_key]["priority"]).strip()
    if priority not in {"Low", "Normal", "High", "Emergency"}:
        raise ValueError("Choose a valid priority.")
    color = str(raw.get("color") or ALERT_SPECS[alert_key]["color"]).strip().lstrip("#").upper()
    if not re.fullmatch(r"[0-9A-F]{6}", color):
        raise ValueError("Color must be a 6 digit hex value.")
    icon = str(raw.get("icon") or "").strip()
    expires = str(raw.get("expires") or "").strip() or "alert"
    audio = [str(item).strip() for item in (raw.get("audio") or []) if str(item).strip()]
    vendor_specific = str(raw.get("vendor_specific") or "").strip()
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex),
        "alert_key": alert_key,
        "zones": zones,
        "shortmessage": shortmessage,
        "longmessage": longmessage,
        "priority": priority,
        "color": color,
        "icon": icon,
        "audio": audio,
        "expires": expires,
        "vendor_specific": vendor_specific,
    }


def default_entry_for_alert(alert_key):
    spec = ALERT_SPECS[alert_key]
    shortmessage = "${alertname} issued"
    if alert_key == "tornado_warning":
        shortmessage = "SHELTER! For a tornado. Go to your tornado shelter. - TORNADO WARNING ISSUED"
    return {
        "id": uuid.uuid4().hex,
        "alert_key": alert_key,
        "zones": [],
        "shortmessage": shortmessage,
        "longmessage": "National Weather Service has issued a ${alertname} for ${locations}\n${alerttext}",
        "priority": spec["priority"],
        "color": spec["color"],
        "icon": "",
        "audio": [],
        "expires": "alert",
        "vendor_specific": "",
    }


def combined_vendor_specific(entry):
    priority = str(entry.get("priority") or "").strip().lower()
    try:
        data = json.loads(entry.get("vendor_specific") or "")
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    if priority != "emergency":
        any_values = dict(data.get("any") or {})
        any_values["noPersistentScroll"] = True
        data["any"] = any_values
    if not data:
        return ""
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def module_vendor_specific(priority):
    if str(priority or "").strip().lower() == "emergency":
        return ""
    return json.dumps({"any": {"noPersistentScroll": True}}, separators=(",", ":"), sort_keys=True)


def alert_expiry_expires(alert):
    props = dict(alert.get("properties") or {})
    dt = parse_iso8601(props.get("expires") or props.get("ends"))
    if dt is None:
        return "manual"
    delta = dt.astimezone(timezone.utc) - now_utc()
    seconds = delta.total_seconds()
    if seconds <= 0:
        return "1m"
    minutes = max(1, int((seconds + 59) // 60))
    return f"{minutes}m"


def expand_expires(raw, alert):
    text = str(raw or "manual").strip() or "manual"
    tokens = [token for token in text.split("|") if token]
    if "alert" not in tokens and "alertm" not in tokens:
        return text
    after = alert_expiry_expires(alert)
    out = []
    for token in tokens:
        if token in ("alert", "alertm"):
            if after and after != "manual":
                out.append(after)
        else:
            out.append(token)
    return "|".join(out) if out else "manual"


def substitute_template(template, variables):
    result = str(template or "")
    for key, value in variables.items():
        result = result.replace("${" + key + "}", str(value or ""))
    return result


def alert_variables(alert):
    props = dict(alert.get("properties") or {})
    description = str(props.get("description") or "").strip()
    instruction = str(props.get("instruction") or "").strip()
    if description and instruction and instruction not in description:
        alert_text = description + "\n\n" + instruction
    else:
        alert_text = description or instruction
    expiration_text = format_expiration(props.get("expires") or props.get("ends"))
    event_name = resolved_alert_display_name(alert)
    return {
        "alertname": event_name,
        "alertnamed": event_name,
        "alerttext": alert_text,
        "locations": str(props.get("areaDesc") or "").strip(),
        "expiration": expiration_text,
    }


def extract_alert_id(alert):
    props = dict(alert.get("properties") or {})
    return str(props.get("id") or alert.get("id") or "").strip()


def extract_alert_zone_codes(alert):
    props = dict(alert.get("properties") or {})
    found = []
    geocode = props.get("geocode") or {}
    for value in geocode.get("UGC") or []:
        token = str(value or "").strip().upper()
        if token and token not in found:
            found.append(token)
    for value in props.get("affectedZones") or []:
        tail = str(value or "").rstrip("/").split("/")[-1].strip().upper()
        if tail and tail not in found:
            found.append(tail)
    return found


def alert_matches_entry(alert, entry):
    props = dict(alert.get("properties") or {})
    event = str(props.get("event") or "").strip().lower()
    spec = ALERT_SPECS.get(str(entry.get("alert_key") or ""))
    if not spec:
        return False
    if event != spec["event"].lower():
        return False
    alert_zones = set(extract_alert_zone_codes(alert))
    entry_zones = {str(item or "").strip().upper() for item in entry.get("zones") or []}
    return bool(alert_zones & entry_zones)


def alert_has_destructive_damage_threat(alert):
    props = dict(alert.get("properties") or {})
    params = props.get("parameters") or {}
    for key, values in params.items():
        if "damagethreat" not in str(key or "").lower():
            continue
        if not isinstance(values, list):
            values = [values]
        for value in values:
            if "DESTRUCTIVE" in str(value or "").upper():
                return True
    description = str(props.get("description") or "")
    headline = str(props.get("headline") or "")
    combined = f"{headline}\n{description}".upper()
    return "DAMAGE THREAT...DESTRUCTIVE" in combined or "DESTRUCTIVE" in combined


def resolved_alert_display_name(alert):
    props = dict(alert.get("properties") or {})
    event = str(props.get("event") or "").strip()
    if event.lower() == "severe thunderstorm warning" and alert_has_destructive_damage_threat(alert):
        return "Severe Thunderstorm Warning - Destructive"
    return event


def render_audio_sequence(entry, alert):
    stored = [str(item).strip() for item in (entry.get("audio") or []) if str(item).strip()]
    if not stored:
        return build_audio_sequence(entry, alert)
    variables = alert_variables(alert)
    try:
        from tts import decode_tts_token, encode_tts_token
    except Exception:
        decode_tts_token = None
        encode_tts_token = None
    rendered = []
    for value in stored:
        payload = None
        if decode_tts_token is not None:
            try:
                payload = decode_tts_token(value)
            except Exception:
                payload = None
        if payload and encode_tts_token is not None:
            text = substitute_template(payload.get("text") or "", variables)
            try:
                rendered.append(encode_tts_token({"engine": payload.get("engine"), "voice": payload.get("voice"), "text": text}))
                continue
            except Exception:
                pass
        rendered.append(value)
    return rendered


def load_audio_candidates():
    try:
        from srv.web import app as webapp
        return set(webapp.audio_files())
    except Exception:
        return set()


def default_tts_token(text):
    try:
        from tts import available_tts_voices, encode_tts_token
    except Exception:
        return None
    try:
        voices = available_tts_voices()
    except Exception:
        voices = []
    chosen = None
    for voice in voices:
        if str(voice.get("engine") or "").strip().lower() != "google":
            chosen = voice
            break
    if chosen is None:
        for preferred in ("en", "en-us", "en-gb"):
            for voice in voices:
                if str(voice.get("engine") or "").strip().lower() == "google" and str(voice.get("voice") or "").strip().lower() == preferred:
                    chosen = voice
                    break
            if chosen is not None:
                break
    if chosen is None:
        for voice in voices:
            if str(voice.get("engine") or "").strip().lower() == "google":
                chosen = voice
                break
    if chosen is None:
        return None
    try:
        return encode_tts_token({"engine": chosen.get("engine"), "voice": chosen.get("voice"), "text": text})
    except Exception:
        return None


def build_audio_sequence(entry, alert):
    variables = alert_variables(alert)
    alertname = variables["alertname"]
    locations = variables["locations"]
    tts_text = f"National Weather Service has issued a {alertname} for {locations}".strip()
    priority = str(entry.get("priority") or "").strip()
    if str(entry.get("alert_key") or "") == "tornado_warning":
        tts_text = f"SHELTER! For a tornado. Go to your tornado shelter. National Weather Service has issued a Tornado Warning for {locations}".strip()
    available = load_audio_candidates()
    entries = []
    if priority == "High":
        if "OPS-900HZ-SlowPulse.wav" in available:
            entries.append("OPS-900HZ-SlowPulse.wav")
        repeat = 3
    elif priority == "Emergency":
        if "OPS-400HZ-MedPulse.wav" in available:
            entries.append("OPS-400HZ-MedPulse.wav")
        repeat = 5
    else:
        repeat = 1
    token = default_tts_token(tts_text)
    if token:
        entries.extend([token] * repeat)
    return entries


def build_broadcast_values(endpoint_name, entry, alert):
    variables = alert_variables(alert)
    shortmessage = substitute_template(entry.get("shortmessage") or "", variables)
    longmessage = substitute_template(entry.get("longmessage") or "", variables)
    expires = expand_expires(entry.get("expires"), alert)
    return {
        "name": str(endpoint_name or "").strip(),
        "shortmessage": shortmessage,
        "longmessage": longmessage,
        "color": str(entry.get("color") or "").strip(),
        "priority": str(entry.get("priority") or "Normal").strip(),
        "audio": ":".join(render_audio_sequence(entry, alert)),
        "vendor_specific": combined_vendor_specific(entry),
        "expires": expires,
        "icon": str(entry.get("icon") or "").strip(),
    }


def state_label(code):
    code = str(code or "").strip().upper()
    return STATE_NAMES.get(code, code or "Other")


def zone_sort_key(item):
    return (
        state_label(item.get("state")).lower(),
        str(item.get("kind") or "").lower(),
        str(item.get("name") or "").lower(),
        str(item.get("code") or "").lower(),
    )


def fetch_json(session, url):
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_paginated_features(session, url):
    features = []
    current = url
    while current:
        payload = fetch_json(session, current)
        for feature in payload.get("features") or []:
            if isinstance(feature, dict):
                features.append(feature)
        pagination = payload.get("pagination") or {}
        current = pagination.get("next")
    return features


def fetch_zone_catalog():
    session = requests_session()
    zones = []
    for url, kind in ((COUNTY_ZONE_URL, "County"), (MARINE_ZONE_URL, "Marine")):
        for feature in fetch_paginated_features(session, url):
            props = dict(feature.get("properties") or {})
            code = str(props.get("id") or feature.get("id") or "").strip().upper()
            if not code:
                continue
            state = str(props.get("state") or code[:2]).strip().upper()
            zones.append(
                {
                    "code": code,
                    "name": str(props.get("name") or code).strip(),
                    "state": state,
                    "kind": kind,
                }
            )
    zones = sorted({(item["code"], item["name"], item["state"], item["kind"]): item for item in zones}.values(), key=zone_sort_key)
    grouped = {}
    for item in zones:
        key = item["state"]
        grouped.setdefault(key, []).append(item)
    return {
        "updated_at": now_utc().isoformat(),
        "zones": zones,
        "grouped": grouped,
    }


def load_zone_catalog(force_refresh=False):
    if not force_refresh and ZONE_CACHE_PATH.exists():
        age = time.time() - ZONE_CACHE_PATH.stat().st_mtime
        if age <= ZONE_CACHE_MAX_AGE:
            cached = json_load(ZONE_CACHE_PATH, {})
            if cached.get("zones"):
                return cached
    catalog = fetch_zone_catalog()
    json_dump(ZONE_CACHE_PATH, catalog)
    return catalog


def load_zone_catalog_safe():
    try:
        return load_zone_catalog(force_refresh=False)
    except Exception:
        cached = json_load(ZONE_CACHE_PATH, {})
        if cached.get("zones"):
            return cached
        return {"updated_at": "", "zones": [], "grouped": {}}


def official_alert_type_names():
    session = requests_session()
    payload = fetch_json(session, ALERT_TYPES_URL)
    names = []
    for value in payload.get("eventTypes") or []:
        text = str(value or "").strip()
        if text:
            names.append(text)
    return names
