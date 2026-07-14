#!/usr/bin/env python3
"""
Room sensors publisher (Home Assistant REST API -> broker).

Like publisher_ups/publish_ups.py, this is a one-shot script meant for
cron, not a persistent daemon like publisher_ha/publish.py -- your
Bluetooth thermometer/hygrometer integration in Home Assistant already
polls the devices on its own schedule, so there's nothing to subscribe
to in real time; this just asks HA for the latest state of each sensor
and pushes a formatted table to the broker's "rooms_table" widget (see
layout.yaml -- type: table).

Also optionally pushes a single extra entity (e.g. a radon sensor) to the
"radon_metric" widget in the same run -- see `radon_entity` in
rooms.example.yaml. No separate publisher/cron job needed for it; it just
rides along with the existing rooms fetch since it's the same pattern
(one REST call, no subscription needed).

Uses Home Assistant's REST API (GET /api/states/<entity_id>) rather than
the WebSocket API publisher_ha uses, since a one-shot script doesn't need
a persistent connection. Needs a long-lived access token: in Home
Assistant, click your profile (bottom-left) -> Security tab -> Long-Lived
Access Tokens -> Create Token. This can be the SAME token publisher_ha
uses if you're already running that, or a separate one -- either works.

Config:
    HA_URL              e.g. http://homeassistant.local:8123   (required)
    HA_TOKEN             Home Assistant long-lived access token  (required)
    BROKER_URL           e.g. http://localhost:9090              (required)
    DASHBOARD_TOKEN       same token the broker/other publishers use
    ROOMS_CONFIG          path to rooms.yaml (default: rooms.yaml next to this script)

Usage:
    python3 publish_rooms.py            # fetch + push
    python3 publish_rooms.py --dry-run  # fetch + print payload, don't push

Example crontab (every 5 minutes):
    */5 * * * * HA_URL=http://homeassistant.local:8123 HA_TOKEN=... \
        BROKER_URL=http://localhost:9090 DASHBOARD_TOKEN=... \
        /path/to/publisher_rooms/venv/bin/python3 /path/to/publisher_rooms/publish_rooms.py \
        >> /var/log/rooms_publish.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import yaml

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s publish_rooms: %(message)s"
)
log = logging.getLogger("publish_rooms")


def load_rooms_config(path: Path) -> tuple[list[dict[str, str]], float, Optional[str]]:
    if not path.exists():
        raise RuntimeError(
            f"rooms config not found: {path} "
            "(copy rooms.example.yaml -> rooms.yaml and fill in your entity ids)"
        )
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    rooms = cfg.get("rooms") or []
    if not rooms:
        raise RuntimeError(f"no rooms defined in {path}")
    threshold = float(cfg.get("low_battery_threshold", 20))
    radon_entity = cfg.get("radon_entity") or None
    return rooms, threshold, radon_entity


def fetch_entity_state(ha_url: str, ha_token: str, entity_id: str) -> Optional[str]:
    """Returns the raw state string, or None if unavailable/unreachable --
    callers decide how to render that (we don't want one dead Bluetooth
    sensor to block the other three rooms' data)."""
    url = f"{ha_url.rstrip('/')}/api/states/{entity_id}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        log.warning("HA returned HTTP %s for %s", exc.code, entity_id)
        return None
    except urllib.error.URLError as exc:
        log.warning("couldn't reach HA for %s: %s", entity_id, exc.reason)
        return None

    state = body.get("state")
    if state in (None, "unavailable", "unknown"):
        log.warning("%s is %s in Home Assistant", entity_id, state)
        return None
    return state


def _fmt_number(raw: Optional[str], suffix: str, decimals: int = 0) -> str:
    if raw is None:
        return "--"
    try:
        value = float(raw)
    except ValueError:
        return "--"
    return f"{value:.{decimals}f}{suffix}"


def build_payload(
    ha_url: str,
    ha_token: str,
    rooms: list[dict[str, str]],
    low_battery_threshold: float,
    radon_entity: Optional[str] = None,
) -> dict[str, dict]:
    rows = []
    for room in rooms:
        label = room.get("label", "?")
        temp_raw = fetch_entity_state(ha_url, ha_token, room["temp_entity"])
        humidity_raw = fetch_entity_state(ha_url, ha_token, room["humidity_entity"])
        battery_raw = fetch_entity_state(ha_url, ha_token, room["battery_entity"])

        cells = [
            label,
            _fmt_number(temp_raw, "°C", decimals=1),
            _fmt_number(humidity_raw, "%"),
            _fmt_number(battery_raw, "%"),
        ]

        try:
            is_low_battery = battery_raw is not None and float(battery_raw) <= low_battery_threshold
        except ValueError:
            is_low_battery = False

        if is_low_battery:
            rows.append({"cells": cells, "color": "red"})
        else:
            rows.append(cells)

    payload: dict[str, dict] = {
        "rooms_table": {
            "columns": ["Room", "Temp", "Humidity", "Batt"],
            "rows": rows,
        }
    }

    if radon_entity:
        radon_raw = fetch_entity_state(ha_url, ha_token, radon_entity)
        try:
            radon_value: Any = round(float(radon_raw)) if radon_raw is not None else None
        except ValueError:
            radon_value = None
        payload["radon_metric"] = (
            {"value": radon_value, "unit": " Bq/m³"}
            if radon_value is not None
            else {"value": "--", "unit": ""}
        )

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
    parser = argparse.ArgumentParser(description="Home Assistant room sensors -> broker publisher")
    parser.add_argument(
        "--rooms-config",
        default=os.environ.get("ROOMS_CONFIG", str(BASE_DIR / "rooms.yaml")),
    )
    parser.add_argument("--dry-run", action="store_true", help="fetch + print payload, don't push")
    args = parser.parse_args()

    ha_url = os.environ.get("HA_URL", "")
    ha_token = os.environ.get("HA_TOKEN", "")
    broker_url = os.environ.get("BROKER_URL", "")
    token = os.environ.get("DASHBOARD_TOKEN", "")

    if not ha_url or not ha_token:
        log.error("HA_URL and HA_TOKEN must be set")
        return 1
    if not args.dry_run and not broker_url:
        log.error("BROKER_URL must be set (e.g. http://localhost:9090)")
        return 1

    try:
        rooms, low_battery_threshold, radon_entity = load_rooms_config(Path(args.rooms_config))
        payload = build_payload(ha_url, ha_token, rooms, low_battery_threshold, radon_entity)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    try:
        push_to_broker(broker_url, token, payload)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    extra = " + radon" if "radon_metric" in payload else ""
    log.info("pushed %d room(s)%s to broker", len(payload["rooms_table"]["rows"]), extra)
    return 0


if __name__ == "__main__":
    sys.exit(main())
