import json
import importlib.util
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pymysql
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

NWS_COMMON_REQUIRED = (
    "ACTIVE_ALERT_POLL_INTERVAL",
    "ALERT_SPECS",
    "WARNING_TO_WATCH_EVENT",
    "alert_matches_entry",
    "build_broadcast_values",
    "clamp_poll_interval",
    "endpoint_display_name",
    "extract_alert_id",
    "load_zone_catalog_safe",
    "normalize_entry",
    "normalize_groups_value",
    "requests_session",
)


def load_nws_common():
    candidates = [
        ROOT_DIR / "nws_common.py",
        BASE_DIR / "nws_common_embedded.py",
        BASE_DIR / "nws_common.py",
        ROOT_DIR / "payload" / "nws_common.py",
    ]
    for module_path in candidates:
        if not module_path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("nws_common_runtime", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not all(hasattr(module, name) for name in NWS_COMMON_REQUIRED):
            continue
        return module
    raise FileNotFoundError("nws_common.py was not found in the module package")


nws_common = load_nws_common()
ACTIVE_ALERT_POLL_INTERVAL = nws_common.ACTIVE_ALERT_POLL_INTERVAL
DEFAULT_POLL_INTERVAL = getattr(nws_common, "DEFAULT_POLL_INTERVAL", 180)
DEFAULT_WATCH_POLL_INTERVAL = getattr(nws_common, "DEFAULT_WATCH_POLL_INTERVAL", 60)
WARNING_TO_WATCH_EVENT = nws_common.WARNING_TO_WATCH_EVENT
clamp_poll_interval = nws_common.clamp_poll_interval
endpoint_display_name = nws_common.endpoint_display_name
ALERT_SPECS = nws_common.ALERT_SPECS
alert_matches_entry = nws_common.alert_matches_entry
build_broadcast_values = nws_common.build_broadcast_values
extract_alert_id = nws_common.extract_alert_id
load_zone_catalog_safe = nws_common.load_zone_catalog_safe
normalize_entry = nws_common.normalize_entry
normalize_groups_value = nws_common.normalize_groups_value
requests_session = nws_common.requests_session

try:
    from endpoints import BASE_DIR as OPS_BASE_DIR, MODULE_LOG_DIR
except Exception:
    OPS_BASE_DIR = Path(os.getenv("OPS_PROJECT_ROOT", "/opt/openpagingserver"))
    MODULE_LOG_DIR = Path(os.getenv("OPS_ENDPOINT_MODULE_LOG_DIR", "/var/log/openpagingserver/endpointmodules"))

ENV_PATH = ROOT_DIR.parent.parent / ".env"
load_dotenv(ENV_PATH)

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_NAME = os.getenv("DB_NAME")
MODULE_NAME = "nws"
ENDPOINT_TABLE = "endpoints-input-nws"
ACTIVE_TABLE = "endpoints-input-nws-active"

core = None
running = False
thread = None


def log(message):
    if core and hasattr(core, "log"):
        core.log(message)
    else:
        print(message)


def db():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def ensure_endpoint_columns():
    for column, ddl in (
        ("poll_interval", f"ALTER TABLE `{ENDPOINT_TABLE}` ADD COLUMN `poll_interval` INT NOT NULL DEFAULT {int(DEFAULT_POLL_INTERVAL)}"),
        ("poll_interval_watch", f"ALTER TABLE `{ENDPOINT_TABLE}` ADD COLUMN `poll_interval_watch` INT NOT NULL DEFAULT {int(DEFAULT_WATCH_POLL_INTERVAL)}"),
    ):
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM information_schema.columns "
                    "WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s",
                    (ENDPOINT_TABLE, column),
                )
                row = cur.fetchone()
                if row and int(row.get("c") or 0):
                    continue
                cur.execute(ddl)
            conn.commit()
        except Exception as exc:
            log(f"nws schema migration skipped for {column}: {exc}")
        finally:
            conn.close()


def split_sql_statements(sql):
    statements = []
    current = []
    quote = None
    escape = False
    for char in sql:
        current.append(char)
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
            continue
        if char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement[:-1].strip())
            current = []
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def ensure_database_schema():
    schema_path = BASE_DIR / "install.sql"
    if not schema_path.exists():
        return
    sql_text = schema_path.read_text(encoding="utf-8")
    if core and hasattr(core, "request_table"):
        core.request_table(
            ENDPOINT_TABLE,
            """
            CREATE TABLE IF NOT EXISTS `endpoints-input-nws` (
              `id` INT NOT NULL AUTO_INCREMENT,
              `name` VARCHAR(255) NOT NULL,
              `enabled` TINYINT(1) NOT NULL DEFAULT 1,
              `groups` TEXT DEFAULT NULL,
              `entries_json` LONGTEXT DEFAULT NULL,
              `poll_interval` INT NOT NULL DEFAULT 180,
              `poll_interval_watch` INT NOT NULL DEFAULT 60,
              `last_checked` DATETIME DEFAULT NULL,
              `last_error` TEXT DEFAULT NULL,
              PRIMARY KEY (`id`)
            )
            """,
        )
        ensure_endpoint_columns()
        core.request_table(
            ACTIVE_TABLE,
            """
            CREATE TABLE IF NOT EXISTS `endpoints-input-nws-active` (
              `id` INT NOT NULL AUTO_INCREMENT,
              `endpoint_id` INT NOT NULL,
              `entry_id` VARCHAR(64) NOT NULL,
              `alert_id` VARCHAR(255) NOT NULL,
              `broadcast_id` VARCHAR(64) DEFAULT NULL,
              `last_seen` DATETIME DEFAULT NULL,
              `expires_at` DATETIME DEFAULT NULL,
              PRIMARY KEY (`id`),
              UNIQUE KEY `uk_nws_endpoint_entry_alert` (`endpoint_id`,`entry_id`,`alert_id`)
            )
            """,
        )
    statements = split_sql_statements(sql_text)
    if not statements:
        return
    conn = db()
    try:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
        conn.commit()
    finally:
        conn.close()


def init(core_obj):
    global core, running, thread
    core = core_obj
    running = True
    ensure_database_schema()
    load_zone_catalog_safe()
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


def shutdown():
    global running
    running = False


def fetch_rows():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT `id`, `name`, `enabled`, `groups`, `entries_json`, `poll_interval`, `poll_interval_watch`, `last_checked`, `last_error` "
                f"FROM `{ENDPOINT_TABLE}` ORDER BY `name` ASC, `id` ASC"
            )
            return cur.fetchall()
    finally:
        conn.close()


def parse_entries(raw):
    try:
        loaded = json.loads(raw or "[]")
    except Exception:
        return []
    entries = []
    for item in loaded if isinstance(loaded, list) else []:
        try:
            entries.append(normalize_entry(item))
        except Exception:
            continue
    return entries


def get_endpoint_status():
    endpoints = []
    lookup = nws_common.zone_code_name_lookup()
    for row in fetch_rows():
        entries = parse_entries(row.get("entries_json"))
        endpoints.append(
            {
                "id": f"nws-{row.get('id')}",
                "name": endpoint_display_name(entries, lookup),
                "address": "",
                "model": "",
                "status": "",
                "status_state": "",
                "type": "",
                "direction": "Input",
                "input_capable": True,
                "capabilities": ["input"],
            }
        )
    return {
        "module": MODULE_NAME,
        "display_name": "NWS Alerts",
        "input_type": "Input",
        "endpoints": endpoints,
    }


def update_endpoint_check(endpoint_id, error=""):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE `{ENDPOINT_TABLE}` SET `last_checked`=NOW(), `last_error`=%s WHERE `id`=%s",
                (str(error or ""), endpoint_id),
            )
        conn.commit()
    finally:
        conn.close()


def fetch_active_state():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT `endpoint_id`, `entry_id`, `alert_id`, `broadcast_id` FROM `{ACTIVE_TABLE}`"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    active = {}
    for row in rows:
        active[(str(row.get("endpoint_id")), str(row.get("entry_id")), str(row.get("alert_id")))] = row
    return active


def upsert_active_state(endpoint_id, entry_id, alert_id, broadcast_id, expires_at):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO `{ACTIVE_TABLE}` (`endpoint_id`, `entry_id`, `alert_id`, `broadcast_id`, `last_seen`, `expires_at`) "
                "VALUES (%s,%s,%s,%s,NOW(),%s) "
                "ON DUPLICATE KEY UPDATE `broadcast_id`=VALUES(`broadcast_id`), `last_seen`=NOW(), `expires_at`=VALUES(`expires_at`)",
                (endpoint_id, entry_id, alert_id, broadcast_id, expires_at),
            )
        conn.commit()
    finally:
        conn.close()


def prune_active_state(valid_keys):
    valid = {(str(a), str(b), str(c)) for a, b, c in valid_keys}
    try:
        from broadcasts import mark_active_broadcast_delivery
    except Exception:
        mark_active_broadcast_delivery = None
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT `endpoint_id`, `entry_id`, `alert_id`, `broadcast_id` FROM `{ACTIVE_TABLE}`")
            rows = cur.fetchall()
            for row in rows:
                key = (str(row.get("endpoint_id")), str(row.get("entry_id")), str(row.get("alert_id")))
                if key in valid:
                    continue
                broadcast_id = str(row.get("broadcast_id") or "").strip()
                if broadcast_id and mark_active_broadcast_delivery is not None:
                    try:
                        mark_active_broadcast_delivery(broadcast_id, "expired")
                    except Exception as exc:
                        log(f"nws expire broadcast error broadcast={broadcast_id}: {exc}")
                cur.execute(
                    f"DELETE FROM `{ACTIVE_TABLE}` WHERE `endpoint_id`=%s AND `entry_id`=%s AND `alert_id`=%s",
                    key,
                )
        conn.commit()
    finally:
        conn.close()


def fetch_alerts():
    payload = requests_session().get("https://api.weather.gov/alerts/active", timeout=30)
    payload.raise_for_status()
    data = payload.json()
    return list(data.get("features") or [])


def send_custom_message(groups, values):
    from broadcasts import create_custom_broadcast, expire_any_message_rule_broadcasts, expire_message_rule_broadcasts
    from endpoints import ensure_message_vendor_schema, ensure_module_can_send, enforce_input_module_send_rate_limit, resolve_sender_value, validate_group_value, validate_message_priority

    ensure_module_can_send(MODULE_NAME)
    enforce_input_module_send_rate_limit(MODULE_NAME)
    ensure_message_vendor_schema()
    conn = db()
    try:
        with conn.cursor() as cur:
            groups_value = validate_group_value(cur, groups)
            sender = resolve_sender_value(cur, sender=f"{MODULE_NAME.upper()} Alerts")
            values = dict(values or {})
            priority = validate_message_priority(values.get("priority") or "Normal")
            values["priority"] = priority or "Normal"
            values["name"] = values.get("name") or "NWS Alert"
            broadcast_id, expires_rule = create_custom_broadcast(cur, values, groups=groups_value, sender=sender)
            if str(values.get("priority") or "").strip().lower() != "emergency":
                expire_message_rule_broadcasts(cur, expires_rule, [broadcast_id], trigger_groups=groups_value)
                expire_any_message_rule_broadcasts(cur, [broadcast_id], trigger_groups=groups_value)
        conn.commit()
        return broadcast_id
    finally:
        conn.close()


def endpoint_rows():
    rows = []
    for row in fetch_rows():
        if not int(row.get("enabled") or 0):
            continue
        groups = normalize_groups_value(row.get("groups") or "")
        entries = parse_entries(row.get("entries_json"))
        if not groups or not entries:
            continue
        rows.append(
            {
                "id": int(row.get("id")),
                "name": str(row.get("name") or "").strip(),
                "groups": groups,
                "entries": entries,
                "poll_interval": clamp_poll_interval(row.get("poll_interval"), DEFAULT_POLL_INTERVAL),
                "poll_interval_watch": clamp_poll_interval(row.get("poll_interval_watch"), DEFAULT_WATCH_POLL_INTERVAL),
            }
        )
    return rows


def process_endpoint(endpoint, alerts, existing_active):
    valid_keys = set()
    endpoint_error = ""
    for entry in endpoint.get("entries") or []:
        for alert in alerts:
            if not alert_matches_entry(alert, entry):
                continue
            alert_id = extract_alert_id(alert)
            if not alert_id:
                continue
            key = (str(endpoint["id"]), str(entry["id"]), str(alert_id))
            valid_keys.add(key)
            if key in existing_active:
                upsert_active_state(endpoint["id"], entry["id"], alert_id, existing_active[key].get("broadcast_id"), None)
                continue
            values = build_broadcast_values(endpoint.get("name") or "NWS Alert", entry, alert)
            try:
                broadcast_id = send_custom_message(endpoint.get("groups"), values)
                upsert_active_state(endpoint["id"], entry["id"], alert_id, broadcast_id, None)
            except Exception as exc:
                endpoint_error = str(exc)
                log(f"nws send error endpoint={endpoint['id']} entry={entry['id']} alert={alert_id}: {exc}")
    update_endpoint_check(endpoint["id"], endpoint_error)
    return valid_keys


def active_watch_events(alerts):
    events = set()
    for alert in alerts or []:
        props = alert.get("properties") or {}
        event = str(props.get("event") or "").strip().lower()
        if event:
            events.add(event)
    return events


def compute_poll_interval(rows, alerts):
    if not rows:
        return DEFAULT_POLL_INTERVAL
    watch_events = active_watch_events(alerts)
    fast_intervals = []
    base_intervals = []
    for endpoint in rows:
        base_intervals.append(endpoint.get("poll_interval") or DEFAULT_POLL_INTERVAL)
        configured_warnings = {
            str(entry.get("alert_key") or "")
            for entry in endpoint.get("entries") or []
        }
        for warning_key in configured_warnings:
            watch_event = WARNING_TO_WATCH_EVENT.get(warning_key)
            if watch_event and watch_event in watch_events:
                fast_intervals.append(endpoint.get("poll_interval_watch") or DEFAULT_WATCH_POLL_INTERVAL)
                break
    if fast_intervals:
        return min(fast_intervals)
    return min(base_intervals) if base_intervals else DEFAULT_POLL_INTERVAL


def loop():
    while running:
        interval = DEFAULT_POLL_INTERVAL
        try:
            alerts = fetch_alerts()
            existing_active = fetch_active_state()
            rows = endpoint_rows()
            valid_keys = set()
            for endpoint in rows:
                valid_keys.update(process_endpoint(endpoint, alerts, existing_active))
            prune_active_state(valid_keys)
            interval = compute_poll_interval(rows, alerts)
        except Exception as exc:
            log(f"nws poll error: {exc}")
        for _ in range(max(1, int(interval))):
            if not running:
                break
            time.sleep(1)
