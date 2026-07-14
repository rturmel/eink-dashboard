#!/usr/bin/env python3
"""
Zabbix publisher (Zabbix JSON-RPC API -> broker).

A one-shot script meant to be run from cron on its own schedule --
independent of publisher_ups/publisher_rooms, with its own crontab line
and its own log file. Zabbix already polls its monitored hosts
continuously, so there's nothing to subscribe to in real time; each run
logs in, asks for the latest value of a handful of items on one host, and
pushes them to the broker.

Pure standard library on purpose -- no venv/pip install needed, just
python3. Talks to https://zabbix.example.com/api_jsonrpc.php the same way
the Zabbix frontend does.

Widgets pushed (see docs/WIDGETS.md for the payload shape of each type;
prefix each id with ZABBIX_WIDGET_PREFIX if you set one, default ""):
    raid_status  (metric)    -- raid.status item, text value (red if not
                                 a recognized "OK" state -- see
                                 OK_RAID_STATUSES below, adjust to match
                                 whatever your actual item reports)
    raid_sync    (metric)    -- raid.sync item, 0-100 -- plain number, no
                                 bar: it's either "done" (100) or "in
                                 progress at N%", a bar doesn't add
                                 anything the number doesn't already say
    cpu_util     (progress)  -- system.cpu.util item, 0-100
    disk_pie     (pie_chart) -- vfs.fs.dependent.size[/raid-data,pused]
                                 item, Used/Free split of /raid-data

Add matching entries to your layout.yaml with these ids -- see
docs/SETUP.md for a copy-paste layout snippet.

Config is entirely environment variables (no config file, so nothing here
ever accidentally commits credentials):
    ZABBIX_URL            e.g. http://192.168.11.75:8080/api_jsonrpc.php  (required)
    ZABBIX_USER           Zabbix account username (required)               -- use a
                           read-only account, not an admin one
    ZABBIX_PASSWORD       that account's password (required)
    ZABBIX_HOST           technical host name in Zabbix, e.g. "serval"     (required)
    BROKER_URL            e.g. http://localhost:9090                       (required)
    DASHBOARD_TOKEN        same token the broker/other publishers use       (required
                           unless the broker has no token configured)
    ZABBIX_WIDGET_PREFIX  default "" (ids: raid_status, raid_sync, cpu_util)

Usage:
    python3 publish_zabbix.py            # log in, fetch, push
    python3 publish_zabbix.py --dry-run  # log in, fetch, print payload, don't push

Example crontab (every 5 minutes, independent of the other publishers'
cron lines/log files):
    */5 * * * * ZABBIX_URL=http://192.168.11.75:8080/api_jsonrpc.php \\
        ZABBIX_USER=zabbix_api ZABBIX_PASSWORD=... ZABBIX_HOST=serval \\
        BROKER_URL=http://localhost:9090 DASHBOARD_TOKEN=... \\
        /usr/bin/python3 /path/to/eink_dashboard/publisher_zabbix/publish_zabbix.py \\
        >> /var/log/zabbix_publish.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s publish_zabbix: %(message)s"
)
log = logging.getLogger("publish_zabbix")

# The item keys this script asks Zabbix for, on ZABBIX_HOST. Edit this
# (and build_payload() below) if you want more/different items later.
ITEM_KEYS = (
    "raid.status",
    "raid.sync",
    "system.cpu.util",
    "vfs.fs.dependent.size[/raid-data,pused]",
)

# raid.status values (lowercased) that mean "all good" -- anything else
# draws the widget in red. This is a guess at common wording; once you see
# what your actual item reports (run with --dry-run), add/adjust entries
# here to match.
OK_RAID_STATUSES = {"ok", "healthy", "optimal", "clean", "active", "normal", "good"}


class ZabbixError(RuntimeError):
    pass


def _rpc(
    api_url: str, method: str, params: dict[str, Any], request_id: int, auth_token: Optional[str] = None
) -> Any:
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}
    headers = {"Content-Type": "application/json-rpc"}
    if auth_token:
        # Zabbix >=6.4 wants the token in this header and deprecates the
        # "auth" body param below; older versions ignore the header and
        # need "auth" in the body instead. Sending both covers either.
        headers["Authorization"] = f"Bearer {auth_token}"
        body["auth"] = auth_token

    req = urllib.request.Request(
        api_url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ZabbixError(f"HTTP {exc.code} calling {method}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ZabbixError(f"couldn't reach Zabbix at {api_url}: {exc.reason}") from exc

    if "error" in result:
        err = result["error"]
        raise ZabbixError(f"Zabbix API error calling {method}: {err.get('data') or err.get('message')}")
    return result["result"]


def login(api_url: str, username: str, password: str) -> str:
    """Zabbix 6.4 renamed user.login's "user" param to "username"; try the
    new name first and fall back to the old one so this works against
    either an old or a current Zabbix server without needing to know
    which in advance."""
    last_exc: Optional[Exception] = None
    for field in ("username", "user"):
        try:
            return _rpc(api_url, "user.login", {field: username, "password": password}, 1)
        except ZabbixError as exc:
            last_exc = exc
            continue
    raise ZabbixError(f"login failed (tried both 'username' and 'user' params): {last_exc}")


def logout(api_url: str, token: str) -> None:
    try:
        _rpc(api_url, "user.logout", {}, 99, token)
    except ZabbixError as exc:
        # Not fatal -- the session expires on its own either way.
        log.warning("logout failed (harmless): %s", exc)


def get_host_id(api_url: str, token: str, host: str) -> str:
    hosts = _rpc(
        api_url, "host.get", {"filter": {"host": [host]}, "output": ["hostid", "host"]}, 2, token
    )
    if not hosts:
        raise ZabbixError(
            f"host {host!r} not found -- check the exact technical host name in "
            "Zabbix (Data collection > Hosts), not its visible display name"
        )
    return hosts[0]["hostid"]


def get_items(api_url: str, token: str, hostid: str, keys: tuple[str, ...]) -> dict[str, dict]:
    items = _rpc(
        api_url,
        "item.get",
        {"hostids": [hostid], "filter": {"key_": list(keys)}, "output": ["key_", "lastvalue", "name"]},
        3,
        token,
    )
    by_key = {item["key_"]: item for item in items}
    missing = [k for k in keys if k not in by_key]
    if missing:
        log.warning("item(s) not found on host: %s", ", ".join(missing))
    return by_key


def _as_float(raw: str) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def build_payload(by_key: dict[str, dict], prefix: str) -> dict[str, dict]:
    payload: dict[str, dict] = {}

    status_item = by_key.get("raid.status")
    if status_item is not None:
        status_text = str(status_item.get("lastvalue", "")).strip()
        is_ok = status_text.lower() in OK_RAID_STATUSES
        payload[f"{prefix}raid_status"] = {
            "label": "RAID",
            "value": status_text or "--",
            "color": "black" if is_ok else "red",
        }

    # raid_sync is a plain metric (no bar) -- still flagged red under 100%
    # so an in-progress rebuild is visible at a glance.
    sync_item = by_key.get("raid.sync")
    if sync_item is not None:
        sync_value = _as_float(sync_item.get("lastvalue"))
        if sync_value is not None:
            payload[f"{prefix}raid_sync"] = {
                "label": "Sync",
                "value": round(sync_value),
                "unit": "%",
                "color": "red" if sync_value < 100 else "black",
            }

    # progress's own default ("red" under 20%) assumes a battery-style
    # metric where LOW is bad -- cpu_util is the opposite (HIGH is bad),
    # so a perfectly normal 17% CPU load needs an explicit color or it'd
    # render red.
    cpu_item = by_key.get("system.cpu.util")
    if cpu_item is not None:
        cpu_value = _as_float(cpu_item.get("lastvalue"))
        if cpu_value is not None:
            payload[f"{prefix}cpu_util"] = {
                "label": "CPU Util",
                "value": round(cpu_value),
                "color": "red" if cpu_value >= 90 else "black",
            }

    # vfs.fs.dependent.size[...,pused] reports percent USED directly --
    # Free is just the complement. Rounding used first and deriving free
    # from that (rather than rounding both independently) guarantees the
    # two segments always sum to exactly 100.
    disk_item = by_key.get("vfs.fs.dependent.size[/raid-data,pused]")
    if disk_item is not None:
        used_value = _as_float(disk_item.get("lastvalue"))
        if used_value is not None:
            used_pct = round(used_value)
            payload[f"{prefix}disk_pie"] = {
                "title": "/raid-data",
                "segments": [
                    {"label": "Used", "value": used_pct, "color": "red" if used_pct >= 90 else "black"},
                    {"label": "Free", "value": 100 - used_pct, "color": "white"},
                ],
            }

    return payload


def push_to_broker(broker_url: str, token: str, payload: dict[str, dict]) -> None:
    url = f"{broker_url.rstrip('/')}/api/v1/widgets/bulk"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"broker rejected push: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"couldn't reach broker at {broker_url}: {exc.reason}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Zabbix -> broker publisher")
    parser.add_argument("--dry-run", action="store_true", help="fetch + print payload, don't push")
    args = parser.parse_args()

    api_url = os.environ.get("ZABBIX_URL", "")
    username = os.environ.get("ZABBIX_USER", "")
    password = os.environ.get("ZABBIX_PASSWORD", "")
    host = os.environ.get("ZABBIX_HOST", "")
    broker_url = os.environ.get("BROKER_URL", "")
    token = os.environ.get("DASHBOARD_TOKEN", "")
    prefix = os.environ.get("ZABBIX_WIDGET_PREFIX", "")

    missing = [
        name
        for name, val in (
            ("ZABBIX_URL", api_url),
            ("ZABBIX_USER", username),
            ("ZABBIX_PASSWORD", password),
            ("ZABBIX_HOST", host),
        )
        if not val
    ]
    if missing:
        log.error("missing required env var(s): %s", ", ".join(missing))
        return 1
    if not args.dry_run and not broker_url:
        log.error("BROKER_URL is not set (e.g. http://localhost:9090)")
        return 1

    auth_token = None
    try:
        auth_token = login(api_url, username, password)
        hostid = get_host_id(api_url, auth_token, host)
        by_key = get_items(api_url, auth_token, hostid, ITEM_KEYS)
        payload = build_payload(by_key, prefix)
    except ZabbixError as exc:
        log.error("%s", exc)
        return 1
    finally:
        if auth_token:
            logout(api_url, auth_token)

    if not payload:
        log.error("none of %s resolved to a value -- nothing to push", ITEM_KEYS)
        return 1

    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    try:
        push_to_broker(broker_url, token, payload)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    log.info("pushed %d widget(s) to broker: %s", len(payload), list(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
