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
    WEATHER_MODEL          default "gem_seamless" -- Environment Canada's own
                            GEM model, the same source ECCC/Meteomedia use, so
                            values match those sites far more closely than
                            Open-Meteo's global "Best Match" default does for
                            a Canadian city. Set to "" to use Best Match
                            instead, or any other model name from
                            https://open-meteo.com/en/docs if you're not in
                            Canada. Falls back to Best Match automatically if
                            the chosen model doesn't support a requested
                            field (e.g. GEM has no UV index).
    BROKER_URL              e.g. http://localhost:9090          (required)
    DASHBOARD_TOKEN         same token the broker/other publishers use
                            (required unless the broker has no token
                            configured)
    WEATHER_WIDGET_PREFIX  default ""
    WEATHER_SANITY_CHECK   default "true" -- after fetching, cross-checks the
                            model's current temperature/humidity against the
                            nearest real Environment Canada station
                            observation (api.weather.gc.ca's swob-realtime
                            feed -- actual instrument readings, not another
                            forecast) and logs a warning if they diverge by
                            more than WEATHER_SANITY_THRESHOLD_C. This is
                            advisory only: it never changes what gets pushed
                            to the dashboard, only what gets logged. Set to
                            "false" to skip it entirely (one extra HTTP call
                            per run otherwise).
    WEATHER_SANITY_THRESHOLD_C   default "3.0" -- Celsius delta that triggers
                            a warning log line (temperature is compared in
                            Celsius regardless of TEMPERATURE_UNIT)
    WEATHER_SANITY_RADIUS_KM     default "20" -- how far to search for a
                            real station observation to compare against
    WEATHER_SANITY_MAX_AGE_MIN   default "120" -- ignore station observations
                            older than this (stale readings aren't a
                            meaningful "ground truth")

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
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s publish_weather: %(message)s"
)
log = logging.getLogger("publish_weather")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
# Environment Canada's real-time surface observations (actual instruments --
# airports, co-op stations, buoys -- not a forecast model at all). Used only
# for the optional sanity check below, never for the pushed payload itself.
ECCC_SWOB_URL = "https://api.weather.gc.ca/collections/swob-realtime/items"

# WMO weather codes (the standard Open-Meteo's `weather_code` uses) mapped
# to this project's icon set (shared/dashboard_render/widgets.py's
# _ICON_FN: sunny/cloudy/rain/snow/storm/fog/clear_night).
_WMO_SUNNY = {0, 1}
_WMO_CLOUDY = {2, 3}
_WMO_FOG = {45, 48}
_WMO_RAIN = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}
_WMO_SNOW = {71, 73, 75, 77, 85, 86}
_WMO_STORM = {95, 96, 99}



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


def fetch_forecast(
    lat: float, lon: float, temp_unit: str, forecast_days: int, model: Optional[str] = None
) -> dict[str, Any]:
    """
    `model`, if set, is Open-Meteo's `models` param (e.g. "gem_seamless")
    -- without it, Open-Meteo picks whatever single best global model it
    thinks is best for the coordinates ("Best Match"), which for a
    Canadian location is often NOT Environment Canada's own GEM model, so
    it can read noticeably different from ECCC/Meteomedia (both of which
    are ultimately fed by GEM). "gem_seamless" blends GEM's high-res
    2.5km HRDPS (short range) with its coarser Regional/Global runs
    (longer range) into one series -- the closest match to what Canadian
    sources show, without HRDPS's 2-day forecast cutoff leaving later
    days empty.

    Not every variable is available from every model (GEM doesn't compute
    UV index, for instance) -- if the model-specific request 400s, the
    caller should retry without `model` rather than fail outright.
    """
    params: dict[str, Any] = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "current": "temperature_2m,relative_humidity_2m,weather_code,is_day",
        "hourly": "uv_index",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,uv_index_max",
        "timezone": "auto",
        "forecast_days": max(forecast_days + 1, 2),
        "temperature_unit": temp_unit,
    }
    if model:
        params["models"] = model
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


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _parse_swob_time(value: Any) -> Optional[datetime]:
    """SWOB's date_tm is UTC, formatted like "2026-07-16T17:35:00Z" --
    datetime.fromisoformat() (on Python < 3.11, which is what a Pi/older
    Ubuntu box may still ship) chokes on the trailing "Z", so it's swapped
    for an explicit UTC offset first."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_station_observation(
    lat: float, lon: float, radius_km: float, max_age_min: float
) -> Optional[dict[str, Any]]:
    """Returns the nearest real Environment Canada surface observation
    (station name, distance, temperature, humidity, age) within
    `radius_km`, or None if the feed is unreachable or nothing usable is
    nearby -- this is a best-effort sanity check, not a hard dependency,
    so any failure here should never break the main publish flow.

    Queried from api.weather.gc.ca's swob-realtime collection -- actual
    instrument readings from airports/co-op stations/buoys, not another
    forecast model, which is what makes it useful as an independent check
    on Open-Meteo's model output rather than just comparing one forecast
    to another."""
    deg_lat = radius_km / 111.0
    deg_lon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.1))
    bbox = f"{lon - deg_lon},{lat - deg_lat},{lon + deg_lon},{lat + deg_lat}"

    try:
        result = _get_json(ECCC_SWOB_URL, {"bbox": bbox, "limit": 50, "f": "json"})
    except WeatherError as exc:
        log.info("sanity check: couldn't reach ECCC swob-realtime (skipping): %s", exc)
        return None

    now = datetime.now(timezone.utc)
    best = None
    for feature in result.get("features") or []:
        props = feature.get("properties") or {}
        temp = props.get("air_temp")
        if temp is None:
            continue
        observed_at = _parse_swob_time(props.get("date_tm"))
        if observed_at is None:
            continue
        age_min = (now - observed_at).total_seconds() / 60.0
        if age_min < 0 or age_min > max_age_min:
            continue
        coords = (feature.get("geometry") or {}).get("coordinates") or [None, None]
        stn_lon, stn_lat = coords[0], coords[1]
        if stn_lat is None or stn_lon is None:
            continue
        distance_km = _haversine_km(lat, lon, stn_lat, stn_lon)
        if best is None or distance_km < best["distance_km"]:
            best = {
                "station": props.get("stn_nam") or "unknown station",
                "distance_km": distance_km,
                "temp_c": float(temp),
                "humidity": props.get("rel_hum"),
                "age_min": age_min,
            }

    return best


def sanity_check_forecast(
    forecast: dict[str, Any], lat: float, lon: float, temp_unit: str,
    threshold_c: float, radius_km: float, max_age_min: float,
) -> None:
    """Logs a warning (never raises) if the forecast's current
    temperature/humidity diverge from the nearest real ECCC station
    observation by more than `threshold_c`. Advisory only -- this is about
    catching "the model is off today" situations like the discrepancies
    found manually against Meteomedia/Google/meteo.gc.ca during testing,
    without needing someone to notice and paste a screenshot every time."""
    station = fetch_station_observation(lat, lon, radius_km, max_age_min)
    if station is None:
        log.info("sanity check: no usable nearby ECCC station observation found")
        return

    current = forecast.get("current") or {}
    model_temp = current.get("temperature_2m")
    if model_temp is None:
        return
    model_temp_c = (model_temp - 32) * 5 / 9 if temp_unit == "fahrenheit" else model_temp
    temp_delta = model_temp_c - station["temp_c"]

    humidity_note = ""
    model_humidity = current.get("relative_humidity_2m")
    if model_humidity is not None and station["humidity"] is not None:
        humidity_delta = model_humidity - station["humidity"]
        humidity_note = f", humidity model={model_humidity:.0f}% station={station['humidity']:.0f}% (delta {humidity_delta:+.0f}pt)"

    msg = (
        f"model temp={model_temp_c:.1f}°C vs. {station['station']} "
        f"({station['distance_km']:.1f}km away, {station['age_min']:.0f}min old)="
        f"{station['temp_c']:.1f}°C (delta {temp_delta:+.1f}°C){humidity_note}"
    )
    if abs(temp_delta) >= threshold_c:
        log.warning("sanity check: %s -- exceeds %.1f°C threshold", msg, threshold_c)
    else:
        log.info("sanity check: %s", msg)


def _has_real_values(values: Any) -> bool:
    """True if `values` is a non-empty list containing at least one
    non-null entry. GEM accepts the `uv_index` variable name (so the key
    comes back present, with a real-looking non-empty list) but doesn't
    actually compute it -- it fills every slot with `null` rather than
    omitting the key or erroring. A plain truthiness/emptiness check
    treats that null-filled list as "data present" and never looks
    further, which is how UV silently disappeared even though nothing
    raised an error."""
    return isinstance(values, list) and any(v is not None for v in values)


def _current_uv_index(forecast: dict[str, Any]) -> Optional[float]:
    """The forecast API only exposes uv_index via hourly/daily, not
    current -- find the hourly entry closest to the current timestamp;
    fall back to today's daily max only if that lookup can't be done at
    all (a reasonable "how strong does it get today" proxy either way).

    Nearest-match, not exact-match: Open-Meteo's `current` block is
    interpolated to 15-minute granularity ("2026-07-15T10:06") while
    `hourly` only has on-the-hour entries ("2026-07-15T10:00") -- an exact
    string match only succeeds when a run happens to land exactly on the
    hour (~1 in 4 runs), so most of the time this silently fell through to
    the daily *peak* instead of the actual current value -- e.g. reporting
    today's high of 7 at 10am when the real current UV was 3. That's a
    large, systematic overstatement outside peak sun hours, not just
    rounding noise.

    Also skips null entries when picking the nearest hour -- see
    _has_real_values() -- rather than picking the nearest INDEX
    regardless of whether that slot actually has a value.
    """
    current_time = (forecast.get("current") or {}).get("time")
    hourly = forecast.get("hourly") or {}
    times, values = hourly.get("time") or [], hourly.get("uv_index") or []
    if current_time and times and _has_real_values(values):
        try:
            current_dt = datetime.fromisoformat(current_time)
            candidates = [
                i for i in range(min(len(times), len(values))) if values[i] is not None
            ]
            if candidates:
                closest = min(
                    candidates,
                    key=lambda i: abs((datetime.fromisoformat(times[i]) - current_dt).total_seconds()),
                )
                return values[closest]
        except ValueError:
            pass  # malformed timestamp somewhere -- fall through to daily max

    daily = forecast.get("daily") or {}
    daily_uv = daily.get("uv_index_max") or []
    return next((v for v in daily_uv if v is not None), None)


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
        # "Tomorrow" for the first forecast day; every day after that gets
        # its real weekday abbreviation computed from the API's own date
        # (daily_times[i]) rather than a fixed Tomorrow/Wed/Thu/... list --
        # that static list only happened to be correct when run on a
        # Tuesday, since it assumed "the day after tomorrow" is always
        # Wednesday.
        if i == 1:
            label = "Tomorrow"
        else:
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


def _fetch_best_match_supplement(lat: float, lon: float, forecast_days: int) -> dict[str, Any]:
    """A small Best Match (no `models` override) request for the fields
    Environment Canada's GEM model doesn't handle well: current humidity,
    and UV index (hourly + daily). One combined call rather than two
    separate ones, to keep the total request count down.

    Humidity specifically: side-by-side testing for Laval, QC found GEM
    consistently under-reporting relative humidity (58% vs. Best Match's
    67%, which matched other Canadian weather sites) at the same instant
    GEM's own temperature was spot-on -- a genuine per-variable model
    accuracy difference, not a code bug. Different models are good at
    different things; there's no reason to accept GEM's weaker variable
    just because its temperature is the reason it was selected."""
    return _get_json(FORECAST_URL, {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "current": "relative_humidity_2m",
        "hourly": "uv_index",
        "daily": "uv_index_max",
        "timezone": "auto",
        "forecast_days": max(forecast_days + 1, 2),
    })


def fetch_forecast_with_fallback(
    lat: float, lon: float, temp_unit: str, forecast_days: int, model: Optional[str]
) -> dict[str, Any]:
    """fetch_forecast(), but resilient to a specific `model` (e.g. GEM)
    not fully supporting -- or not being particularly accurate at --
    every requested variable:

    - Hard failure (HTTP 400, the whole request rejected) -- falls back
      to Open-Meteo's own "Best Match" selection entirely.
    - Silent partial omission -- GEM doesn't compute UV index at all, and
      Open-Meteo's response for that case is a 200 OK with `uv_index`
      keys present but filled entirely with `null` rather than omitted or
      erroring. That doesn't raise an exception and isn't caught by a
      plain emptiness check either (see _has_real_values()), so it's
      checked for explicitly.
    - Humidity accuracy -- unconditionally overridden from Best Match
      when a specific `model` is in use (see _fetch_best_match_supplement
      for why): GEM being the better source for temperature doesn't make
      it the better source for every other field too.
    """
    if not model:
        return fetch_forecast(lat, lon, temp_unit, forecast_days)

    try:
        forecast = fetch_forecast(lat, lon, temp_unit, forecast_days, model)
    except WeatherError as exc:
        log.warning(
            "forecast request with models=%s failed (%s); retrying with Open-Meteo's "
            "default best-match model instead", model, exc,
        )
        return fetch_forecast(lat, lon, temp_unit, forecast_days)

    needs_uv = not (
        _has_real_values((forecast.get("hourly") or {}).get("uv_index"))
        or _has_real_values((forecast.get("daily") or {}).get("uv_index_max"))
    )
    try:
        supplement = _fetch_best_match_supplement(lat, lon, forecast_days)
    except WeatherError as exc:
        log.warning(
            "Best Match supplement request (humidity%s) failed (continuing with "
            "models=%s's own values): %s", " + UV backfill" if needs_uv else "", model, exc,
        )
        return forecast

    supplement_current = supplement.get("current") or {}
    if supplement_current.get("relative_humidity_2m") is not None:
        forecast.setdefault("current", {})["relative_humidity_2m"] = supplement_current[
            "relative_humidity_2m"
        ]
    if needs_uv:
        forecast["hourly"] = supplement.get("hourly") or forecast.get("hourly")
        forecast.setdefault("daily", {})["uv_index_max"] = (supplement.get("daily") or {}).get(
            "uv_index_max"
        )

    return forecast


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
    # Environment Canada's own GEM model (blended near-term HRDPS +
    # longer-range Regional/Global via "gem_seamless") by default, since
    # this is what ECCC/Meteomedia are themselves built from -- Open-
    # Meteo's global "Best Match" model can read noticeably different for
    # a Canadian city. Set WEATHER_MODEL="" (empty) to opt back into Best
    # Match, or to any other model name from https://open-meteo.com/en/docs
    # if you're not in Canada.
    model = os.environ.get("WEATHER_MODEL", "gem_seamless").strip() or None
    broker_url = os.environ.get("BROKER_URL", "")
    token = os.environ.get("DASHBOARD_TOKEN", "")
    prefix = os.environ.get("WEATHER_WIDGET_PREFIX", "")
    sanity_check = os.environ.get("WEATHER_SANITY_CHECK", "true").strip().lower() not in (
        "false", "0", "no",
    )
    sanity_threshold_c = float(os.environ.get("WEATHER_SANITY_THRESHOLD_C", "3.0"))
    sanity_radius_km = float(os.environ.get("WEATHER_SANITY_RADIUS_KM", "20"))
    sanity_max_age_min = float(os.environ.get("WEATHER_SANITY_MAX_AGE_MIN", "120"))

    if not args.dry_run and not broker_url:
        log.error("BROKER_URL is not set (e.g. http://localhost:9090)")
        return 1

    try:
        lat, lon, location = geocode(city, country_filter)
        forecast = fetch_forecast_with_fallback(lat, lon, temp_unit, forecast_days, model)
        aqi = fetch_air_quality(lat, lon)
        payload = build_payload(forecast, aqi, location, prefix, temp_unit)
    except WeatherError as exc:
        log.error("%s", exc)
        return 1

    if sanity_check:
        # Advisory only -- never let a problem here (network, parsing,
        # whatever) fail the actual publish.
        try:
            sanity_check_forecast(
                forecast, lat, lon, temp_unit,
                sanity_threshold_c, sanity_radius_km, sanity_max_age_min,
            )
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see above
            log.info("sanity check failed unexpectedly (ignoring): %s", exc)

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
