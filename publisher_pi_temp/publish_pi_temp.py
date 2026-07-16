#!/usr/bin/env python3
"""
Pi board temperature publisher (-> broker header subtitle).

Unlike every other publisher in this project, this one is meant to run
**on the Raspberry Pi itself** (the same host as pi_client) -- it reads
the SoC's own temperature sensor, which only exists on that specific
board, not on serval or wherever else a publisher might run. Still a
one-shot script on its own cron line, same pattern as the others.

Reads the temperature two ways, whichever works first:
  1. `vcgencmd measure_temp`     -- ships with Raspberry Pi OS by default,
                                     gives a clean "temp=45.6'C" line.
  2. /sys/class/thermal/thermal_zone0/temp -- a generic Linux sysfs sensor
                                     reading (millidegrees C); used as a
                                     fallback if vcgencmd isn't on PATH
                                     (e.g. running under a minimal
                                     systemd unit, or during --dry-run
                                     testing off the Pi).

Pushes into the **header** widget's `subtitle` field -- nothing else
pushes to `header` in this project, so this is safe: no other publisher's
data gets clobbered. Note this replaces the client's own "Data updated
..." fallback subtitle (see pi_client/client.py / preview/server.py),
since that's only a default used when nothing else has set `subtitle`.

Config is entirely environment variables (no config file):
    TEMPERATURE_UNIT     "celsius" (default) or "fahrenheit"
    PI_TEMP_LABEL         default "Pi" -- text prefix, e.g. "Pi 45.6°C"
    PI_TEMP_WARN_C        default "80" -- Celsius threshold (checked
                           regardless of TEMPERATURE_UNIT) at/above which
                           the subtitle turns red and gets " (warm)"
                           appended. 80°C is where Raspberry Pi's own
                           firmware starts thermally throttling the SoC
                           (85°C is the hard limit) -- this project's
                           dashboard workload is light enough that you
                           should rarely if ever see this in practice, so
                           red here means something's actually worth a
                           look (a stuffy case, blocked vents, etc.), not
                           routine operation. Set to a very high number
                           (e.g. "999") to effectively disable it.
    BROKER_URL             e.g. http://localhost:9090          (required)
    DASHBOARD_TOKEN         same token the broker/other publishers use
                            (required unless the broker has no token
                            configured)

Usage:
    python3 publish_pi_temp.py            # read temp, push
    python3 publish_pi_temp.py --dry-run  # read temp, print payload, don't push

Example crontab (every 5 minutes -- cheap enough to check often; on its
own line/log file like every other publisher here):
    */5 * * * * BROKER_URL=http://localhost:9090 DASHBOARD_TOKEN=... \\
        /usr/bin/python3 /path/to/eink_dashboard/publisher_pi_temp/publish_pi_temp.py \\
        >> /var/log/pi_temp_publish.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s publish_pi_temp: %(message)s"
)
log = logging.getLogger("publish_pi_temp")

THERMAL_ZONE_PATH = "/sys/class/thermal/thermal_zone0/temp"


class PiTempError(RuntimeError):
    pass


def read_cpu_temp_c() -> float:
    """Returns the SoC temperature in Celsius, trying vcgencmd first and
    falling back to the generic Linux sysfs sensor reading."""
    try:
        out = subprocess.run(
            ["vcgencmd", "measure_temp"], capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
        # "temp=45.6'C"
        value = out.split("=", 1)[1].split("'", 1)[0]
        return float(value)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, IndexError, ValueError) as exc:
        log.info("vcgencmd unavailable (%s), falling back to %s", exc, THERMAL_ZONE_PATH)

    try:
        with open(THERMAL_ZONE_PATH, "r") as f:
            millidegrees = int(f.read().strip())
        return millidegrees / 1000.0
    except (OSError, ValueError) as exc:
        raise PiTempError(
            f"couldn't read CPU temperature from vcgencmd or {THERMAL_ZONE_PATH}: {exc}"
        ) from exc


def build_payload(temp_c: float, temp_unit: str, label: str, warn_c: float | None) -> dict[str, dict]:
    if temp_unit == "fahrenheit":
        display_temp = temp_c * 9 / 5 + 32
        unit_symbol = "°F"
    else:
        display_temp = temp_c
        unit_symbol = "°C"

    subtitle = f"{label} {display_temp:.1f}{unit_symbol}"
    is_warm = warn_c is not None and temp_c >= warn_c
    if is_warm:
        subtitle += " (warm)"

    # subtitle_color is set explicitly either way (not just on the warm
    # branch) so a later, cooler reading turns it back to black instead of
    # leaving it stuck red from a past warm reading -- the broker replaces
    # the whole header object each push, but there's nothing else re-pushing
    # a "black" default in between runs.
    return {
        "header": {
            "subtitle": subtitle,
            "subtitle_color": "red" if is_warm else "black",
        }
    }


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
    parser = argparse.ArgumentParser(description="Pi board temperature -> broker header publisher")
    parser.add_argument("--dry-run", action="store_true", help="read temp, print payload, don't push")
    args = parser.parse_args()

    temp_unit = os.environ.get("TEMPERATURE_UNIT", "celsius").strip().lower()
    if temp_unit not in ("celsius", "fahrenheit"):
        log.warning("TEMPERATURE_UNIT %r not recognized, defaulting to celsius", temp_unit)
        temp_unit = "celsius"
    label = os.environ.get("PI_TEMP_LABEL", "Pi")
    # 80°C -- where Raspberry Pi's firmware starts thermally throttling the
    # SoC (85°C is the hard limit). See the module docstring.
    warn_c = float(os.environ.get("PI_TEMP_WARN_C", "80"))
    broker_url = os.environ.get("BROKER_URL", "")
    token = os.environ.get("DASHBOARD_TOKEN", "")

    if not args.dry_run and not broker_url:
        log.error("BROKER_URL is not set (e.g. http://localhost:9090)")
        return 1

    try:
        temp_c = read_cpu_temp_c()
    except PiTempError as exc:
        log.error("%s", exc)
        return 1

    payload = build_payload(temp_c, temp_unit, label, warn_c)

    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    try:
        push_to_broker(broker_url, token, payload)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    log.info("pushed Pi CPU temp: %.1f°C -> %s", temp_c, payload["header"]["subtitle"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
