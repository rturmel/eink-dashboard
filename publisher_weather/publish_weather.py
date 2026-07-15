#!/usr/bin/env python3
"""
Weather publisher (Open-Meteo -> broker).

A one-shot script meant for cron, on its own schedule -- like
publisher_zabbix, independent of the other publishers. Open-Meteo
(https://open-meteo.com) is used for everything here specifically because
its forecast, geocoding, and air-quality APIs are all free and need NO
API key/account -- nothing to sign up for, nothing else to keep secret.

Three HTTP calls per run:
  1. Geocoding API   -- turns WEATHER_CITY into latitude/longitude once
                         (cheap; this runs every time rather than caching,
                         since it's a single lightweight request)
  2. Forecast API    -- current temperature/humidity/condition, today's
                         hi/lo, UV index, and a few days of daily forecast
  3. Air Quality API -- current US AQI

Pure standard library on purpose -- no venv/pip install needed.

Widgets pushed (see docs/WIDGETS.md; prefix each id with
WEATHER_WIDGET_PREFIX if you set one, default ""):
    weather_current   (weather)         -- location, condition, temp, today's
                                            hi/lo, humidity
    weather_stats     (text_list)       -- Humidity, UV Index, Air Quality
                                            (US AQI + category, red if
                                            unhealthy)
    weather_forecast  (forecast_strip)  -- next few days: icon + hi/lo

Config is entirely environment variables (no config file, no API key):
    WEATHER_CITY           default "Laval, Quebec, Canada" -- geocoded each
                            run; be as specific as you like ("Laval, QC" is
                            fine too)
    WEATHER_COUNTRY        optional -- a substring to prefer when the city
                            name is ambiguous (multiple places share a
                            name), matched case-insensitively against the
                            geocoding result's country name, e.g. "Canada"
    TEMPERATURE_UNIT       "celsius" (default) or "fahrenheit"
    FORECAST_DAYS          how many extra days in the forecast strip,
                            default 3 (beyond today)
    BROKER_URL              e.g. http://localhost:9090          (required)
    DASHBOARD_TOKEN         same token the broker/other publishers use
                            (required unless the broker has no token
                            configured)
    WEATHER_WIDGET_PREFIX  default ""

Usage:
    python3 publish_weather.py            # geocode, fetch, push
    python3 publish_weather.py --dry-run  # geocode, fetch, print payload, don't push

Example crontab (every 30 minutes, independent of the other publishers'
cron lines/log files -- weather doesn't need to update as often as sensors):
    */30 * * * * WEATHER_CITY="Laval, Quebec, Canada" \\
        BROKER_URL=http://localhost:9090 DASHBOARD_TOKEN=... \\
        /usr/bin/python3 /path/to/eink_dashboard/publisher_weather/publish_weather.py \\
        >> /var/log/weather_publish.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Optional

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s publish_weather: %(message)s"
)
log = logging.getLogger("publish_weather")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# WMO weather codes (the standard Open-Meteo's `weather_code` uses) mapped
# to this project's icon set (shared/dashboard_render/widgets.py's
# _ICON_FN: sunny/cloudy/rain/snow/storm/fog/clear_night).
_WMO_SUNNY = {0, 1}
_WMO_CLOUDY = {2, 3}
_WMO_FOG = {45, 48}
_WMO_RAIN = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}
_WMO_SNOW = {71, 73, 75, 77, 85, 86}
_WMO_STORM = {95, 96, 99}

_DAY_LABELS = ["Tomorrow", "Wed", "Thu", "Fri", "Sat", "Sun", "Mon", "Tue"]


class WeatherError(RuntimeError):
    pass


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise WeatherError(f"HTTP {exc.code} calling {url}: {exc.reason} -- {body}") from exc
    except urllib.error.URLError as exc:
        raise WeatherError(f"couldn't reach {url}: {exc.reason}") from exc


def geocode(city: str, country_filter: Optional[str]) -> tuple[float, float, str]:
    """Returns (latitude, longitude, display_name).

    Open-Meteo's geocoding `name` param matches place names, not free-text
    "City, Region, Country" strings -- searching for the latter literally
    returns zero results. Only the part before the first comma is actually
    sent (WEATHER_CITY="Laval, Quebec, Canada" queries just "Laval"); use
    WEATHER_COUNTRY to disambiguate if that's not specific enough on its
    own (e.g. multiple cities sharing the name "Laval")."""
    query = city.split(",", 1)[0].strip() or city
    result = _get_json(GEOCODE_URL, {"name": query, "count": 5, "language": "en", "format": "json"})
    candidates = result.get("results") or []
    if not candidates:
        raise WeatherError(f"no geocoding results for {query!r} (parsed from WEATHER_CITY={city!r})")

    match = candidates[0]
    if country_filter:
        needle = country_filter.strip().lower()
        for candidate in candidates:
            if needle in str(candidate.get("country", "")).lower():
                match = candidate
                break

    lat, lon = match["latitude"], match["longitude"]
    admin1 = match.get("admin1")
    display = f"{match['name']}, {admin1}" if admin1 else match["name"]
    return lat, lon, display


def wmo_condition(code: Any, is_day: bool = True) -> str:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "cloudy"
    if code in _WMO_SUNNY:
        return "sunny" if is_day else "clear_night"
    if code in _WMO_CLOUDY:
        return "cloudy"
    if code in _WMO_FOG:
        return "fog"
    if code in _WMO_RAIN:
        return "rain"
    if code in _WMO_SNOW:
        return "snow"
    if code in _WMO_STORM:
        return "storm"
    return "cloudy"


def uv_category(uv: float) -> str:
    if uv < 3:
        return "Low"
    if uv < 6:
        return "Moderate"
    if uv < 8:
        return "High"
    if uv < 11:
        return "Very High"
    return "Extreme"


def aqi_category(aqi: float) -> str:
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Moderate"
    if aqi <= 150:
        return "Unhealthy (Sensitive)"
    if aqi <= 200:
        return "Unhealthy"
    if aqi <= 300:
        return "Very Unhealthy"
    return "Hazardous"


def fetch_forecast(lat: float, lon: float, temp_unit: str, forecast_days: int) -> dict[str, Any]:
    params = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "current": "temperature_2m,relative_humidity_2m,weather_code,is_day",
        "hourly": "uv_index",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,uv_index_max",
        "timezone": "auto",
        "forecast_days": max(forecast_days + 1, 2),
        "temperature_unit": temp_unit,
    }
    return _get_json(FORECAST_URL, params)


def fetch_air_quality(lat: float, lon: float) -> Optional[float]:
    """Returns the current US AQI, or None if unavailable -- air quality is
    treated as optional (nice-to-have) since some regions have sparser
    coverage than the core weather forecast."""
    try:
        result = _get_json(AIR_QUALITY_URL, {
            "latitude": round(lat, 4), "longitude": round(lon, 4), "current": "us_aqi",
        })
    except WeatherError as exc:
        log.warning("air quality lookup failed (continuing without it): %s", exc)
        return None

    current = result.get("current") or {}
    aqi = current.get("us_aqi")
    if aqi is None:
        hourly = result.get("hourly") or {}
        values = hourly.get("us_aqi") or []
        aqi = values[0] if values else None
    return float(aqi) if aqi is not None else None


def _current_uv_index(forecast: dict[str, Any]) -> Optional[float]:
    """The forecast API only exposes uv_index via hourly/daily, not
    current -- find the hourly entry matching the current timestamp; fall
    back to today's daily max if that lookup ever comes up empty (a
    reasonable "how strong does it get today" proxy either way)."""
    current_time = (forecast.get("current") or {}).get("time")
    hourly = forecast.get("hourly") or {}
    times, values = hourly.get("time") or [], hourly.get("uv_index") or []
    if current_time and current_time in times:
        return values[times.index(current_time)]

    daily = forecast.get("daily") or {}
    daily_uv = daily.get("uv_index_max") or []
    return daily_uv[0] if daily_uv else None


def build_payload(
    forecast: dict[str, Any], aqi: Optional[float], location: str, prefix: str, temp_unit: str
) -> dict[str, dict]:
    current = forecast.get("current") or {}
    daily = forecast.get("daily") or {}
    daily_times = daily.get("time") or []
    daily_codes = daily.get("weather_code") or []
    daily_highs = daily.get("temperature_2m_max") or []
    daily_lows = daily.get("temperature_2m_min") or []

    # Derived from the unit WE requested, not parsed back out of the API
    # response -- Open-Meteo's current_units.temperature_2m is already a
    # symbol ("°F"/"°C"), not the word "fahrenheit", so checking it for
    # that substring would never match and this would silently always
    # report Celsius regardless of TEMPERATURE_UNIT.
    temp_unit_symbol = "°F" if temp_unit == "fahrenheit" else "°C"

    is_day = bool(current.get("is_day", 1))
    condition = wmo_condition(current.get("weather_code"), is_day)

    payload: dict[str, dict] = {
        f"{prefix}weather_current": {
            "location": location,
            "condition": condition,
            "temp": round(current.get("temperature_2m", 0)),
            "temp_unit": temp_unit_symbol,
            "high": round(daily_highs[0]) if daily_highs else None,
            "low": round(daily_lows[0]) if daily_lows else None,
            "humidity": current.get("relative_humidity_2m"),
        }
    }

    stats_items = []
    if current.get("relative_humidity_2m") is not None:
        stats_items.append({"label": "Humidity", "value": f"{round(current['relative_humidity_2m'])}%"})

    uv = _current_uv_index(forecast)
    if uv is not None:
        stats_items.append({
            "label": "UV Index",
            "value": f"{uv:.0f} ({uv_category(uv)})",
            "color": "red" if uv >= 8 else "black",
        })

    if aqi is not None:
        stats_items.append({
            "label": "Air Quality",
            "value": f"{aqi:.0f} ({aqi_category(aqi)})",
            "color": "red" if aqi > 100 else "black",
        })

    if stats_items:
        payload[f"{prefix}weather_stats"] = {"items": stats_items}

    forecast_days = []
    for i in range(1, len(daily_times)):
        try:
            label = _DAY_LABELS[i - 1]
        except IndexError:
            try:
                label = datetime.fromisoformat(daily_times[i]).strftime("%a")
            except ValueError:
                label = daily_times[i]
        forecast_days.append({
            "label": label,
            "condition": wmo_condition(daily_codes[i]) if i < len(daily_codes) else "cloudy",
            "high": round(daily_highs[i]) if i < len(daily_highs) else None,
            "low": round(daily_lows[i]) if i < len(daily_lows) else None,
        })

    if forecast_days:
        payload[f"{prefix}weather_forecast"] = {
            "temp_unit": temp_unit_symbol[0],  # just "°", to keep the strip compact
            "days": forecast_days,
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
    parser = argparse.ArgumentParser(description="Open-Meteo -> broker weather publisher")
    parser.add_argument("--dry-run", action="store_true", help="fetch + print payload, don't push")
    args = parser.parse_args()

    city = os.environ.get("WEATHER_CITY", "Laval, Quebec, Canada")
    country_filter = os.environ.get("WEATHER_COUNTRY") or None
    temp_unit = os.environ.get("TEMPERATURE_UNIT", "celsius").strip().lower()
    if temp_unit not in ("celsius", "fahrenheit"):
        log.warning("TEMPERATURE_UNIT %r not recognized, defaulting to celsius", temp_unit)
        temp_unit = "celsius"
    forecast_days = int(os.environ.get("FORECAST_DAYS", "3"))
    broker_url = os.environ.get("BROKER_URL", "")
    token = os.environ.get("DASHBOARD_TOKEN", "")
    prefix = os.environ.get("WEATHER_WIDGET_PREFIX", "")

    if not args.dry_run and not broker_url:
        log.error("BROKER_URL is not set (e.g. http://localhost:9090)")
        return 1

    try:
        lat, lon, location = geocode(city, country_filter)
        forecast = fetch_forecast(lat, lon, temp_unit, forecast_days)
        aqi = fetch_air_quality(lat, lon)
        payload = build_payload(forecast, aqi, location, prefix, temp_unit)
    except WeatherError as exc:
        log.error("%s", exc)
        return 1

    if not payload:
        log.error("nothing resolved to push")
        return 1

    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    try:
        push_to_broker(broker_url, token, payload)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    log.info("pushed weather for %s (%d widget(s)): %s", location, len(payload), list(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
