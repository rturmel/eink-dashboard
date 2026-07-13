#!/usr/bin/env python3
"""
UPS publisher (apcaccess -> broker).

A one-shot script meant to be run from cron every N minutes, not a
long-running daemon like publisher_ha -- apcupsd already polls the UPS
continuously, `apcaccess status` just asks it for the latest snapshot, so
there's nothing to subscribe to. Each run: read `apcaccess status`, parse
a handful of fields, push them to three widgets on the broker, exit.

Pure standard library on purpose -- no venv/pip install needed, just
python3 + the `apcaccess` binary (from the `apcupsd` package) on PATH.

Widgets pushed (see docs/WIDGETS.md for the payload shape of each type):
    <prefix>battery  (progress)      -- battery charge %
    <prefix>load     (metric)        -- UPS load %
    <prefix>alert    (alert_banner)  -- only visible when not ONLINE

Add matching entries to your layout.yaml with these same ids (or set
UPS_WIDGET_PREFIX to something else and match it there) -- see
docs/SETUP.md for a copy-paste layout snippet.

Config is entirely environment variables (no config.yaml, to keep this
dependency-free):
    BROKER_URL          e.g. http://localhost:9090          (required)
    DASHBOARD_TOKEN      same token the broker/other publishers use  (required
                         unless the broker has no token configured)
    APCACCESS_HOST       e.g. 192.168.1.20:3551 -- only needed if apcupsd
                         is running on a different host than this script
    UPS_WIDGET_PREFIX    default "ups_"

Usage:
    python3 publish_ups.py            # parse + push
    python3 publish_ups.py --dry-run  # parse + print payload, don't push

Example crontab (every 2 minutes):
    */2 * * * * BROKER_URL=http://localhost:9090 DASHBOARD_TOKEN=... \
        /usr/bin/python3 /path/to/publisher_ups/publish_ups.py >> /var/log/ups_publish.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s publish_ups: %(message)s"
)
log = logging.getLogger("publish_ups")

# Statuses apcupsd reports that mean "something's wrong, surface it" --
# anything not in this "all clear" set trips the alert banner.
OK_STATUSES = {"ONLINE"}


def run_apcaccess(host: str | None) -> dict[str, str]:
    cmd = ["apcaccess", "status"]
    if host:
        cmd += [host]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=True
        )
    except FileNotFoundError:
        raise RuntimeError(
            "apcaccess not found -- install apcupsd (apt install apcupsd) "
            "and make sure apcaccess is on PATH"
        ) from None
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"apcaccess exited {exc.returncode}: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired:
        raise RuntimeError("apcaccess timed out -- is apcupsd running?") from None

    return parse_apcaccess(result.stdout)


def parse_apcaccess(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    if not fields:
        raise RuntimeError("apcaccess returned no parseable output")
    return fields


def _first_number(value: str) -> float:
    """"100.0 Percent" -> 100.0, "55 Minutes" -> 55.0."""
    match = re.match(r"[-+]?\d*\.?\d+", value)
    if not match:
        raise ValueError(f"no number found in {value!r}")
    return float(match.group())


def build_payload(fields: dict[str, str], prefix: str) -> dict[str, dict]:
    status = fields.get("STATUS", "COMMLOST")
    battery = _first_number(fields.get("BCHARGE", "0"))
    load = _first_number(fields.get("LOADPCT", "0"))
    timeleft = _first_number(fields.get("TIMELEFT", "0")) if "TIMELEFT" in fields else None

    is_ok = status in OK_STATUSES
    if is_ok:
        alert_text = ""
    elif timeleft is not None:
        alert_text = f"UPS on battery ({status}) -- {timeleft:.0f} min left"
    else:
        alert_text = f"UPS status: {status}"

    level = "critical" if status in {"ONBATT", "LOWBATT", "COMMLOST"} else "warning"

    return {
        f"{prefix}battery": {"label": "UPS Battery", "value": round(battery)},
        f"{prefix}load": {"label": "UPS Load", "value": round(load), "unit": "%"},
        f"{prefix}alert": {
            "active": not is_ok,
            "level": level,
            "text": alert_text,
        },
    }


def push_to_broker(broker_url: str, token: str, payload: dict[str, dict]) -> None:
    url = f"{broker_url.rstrip('/')}/api/v1/widgets/bulk"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"broker rejected push: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"couldn't reach broker at {broker_url}: {exc.reason}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="apcupsd -> broker publisher")
    parser.add_argument(
        "--dry-run", action="store_true", help="parse + print payload, don't push"
    )
    args = parser.parse_args()

    broker_url = os.environ.get("BROKER_URL", "")
    token = os.environ.get("DASHBOARD_TOKEN", "")
    apcaccess_host = os.environ.get("APCACCESS_HOST") or None
    prefix = os.environ.get("UPS_WIDGET_PREFIX", "ups_")

    if not args.dry_run and not broker_url:
        log.error("BROKER_URL is not set (e.g. http://localhost:9090)")
        return 1

    try:
        fields = run_apcaccess(apcaccess_host)
        payload = build_payload(fields, prefix)
    except (RuntimeError, ValueError) as exc:
        log.error("%s", exc)
        return 1

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    try:
        push_to_broker(broker_url, token, payload)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    log.info(
        "pushed UPS status (%s, battery %s%%, load %s%%)",
        fields.get("STATUS", "?"),
        payload[f"{prefix}battery"]["value"],
        payload[f"{prefix}load"]["value"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
