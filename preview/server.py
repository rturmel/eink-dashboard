"""
Local web preview of the dashboard.

Connects to the broker exactly like a real Pi client would, but instead of
pushing frames to SPI hardware it renders to PNG and serves a small
auto-refreshing web page -- so you can see what the e-ink panel would show,
from any browser on your network, without waiting on the physical
display's slow (~21s, no partial refresh) redraw cycle.

Run:
    python server.py
Then open http://localhost:9090 (or whatever host/port you configured).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
import websockets
import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent / "shared"))

from dashboard_render import render_dashboard  # noqa: E402
from dashboard_render.fonts import get_font  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s preview: %(message)s"
)
log = logging.getLogger("preview")


def load_config() -> dict[str, Any]:
    cfg_path = Path(os.environ.get("PREVIEW_CONFIG", BASE_DIR / "config.yaml"))
    cfg: dict[str, Any] = {
        "broker_url": "http://localhost:8080",
        "token": "",
        "host": "0.0.0.0",
        "port": 9090,
    }
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg.update(yaml.safe_load(f) or {})
    if os.environ.get("DASHBOARD_TOKEN"):
        cfg["token"] = os.environ["DASHBOARD_TOKEN"]
    if os.environ.get("BROKER_URL"):
        cfg["broker_url"] = os.environ["BROKER_URL"]
    return cfg


CONFIG = load_config()
INDEX_HTML = (BASE_DIR / "templates" / "index.html").read_text()

app = FastAPI(title="E-Ink Dashboard Preview")

_layout: Optional[dict[str, Any]] = None
_state: dict[str, Any] = {}
_frame_bytes: Optional[bytes] = None
_frame_version = 0
# Timestamp of the last time the broker actually told us something changed
# (a "full_state" or "update" WebSocket message) -- distinct from "time" in
# the header, which is just wall-clock render time. Here they'll usually be
# seconds apart since preview re-renders immediately with no debouncing --
# the gap matters far more on pi_client, where "time" reflects the last
# *physical* refresh (which can lag behind by up to min_refresh_interval_
# seconds/force_refresh_seconds) while this reflects when the data itself
# actually last changed.
_last_update_ts: Optional[datetime] = None


def _ws_url() -> str:
    base = CONFIG["broker_url"].rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    if not ws_base.startswith(("ws://", "wss://")):
        ws_base = f"ws://{ws_base}"
    return f"{ws_base}/ws?token={CONFIG.get('token', '')}"


def _render_frame() -> None:
    global _frame_bytes, _frame_version
    if not _layout:
        return
    state = dict(_state)
    header = dict(state.get("header", {}))
    header.setdefault("time", datetime.now().strftime("%-I:%M %p"))
    if _last_update_ts is not None:
        header.setdefault(
            "subtitle", f"Data updated {_last_update_ts.strftime('%b %-d, %-I:%M %p')}"
        )
    state["header"] = header

    image = render_dashboard(_layout, state)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    _frame_bytes = buf.getvalue()
    _frame_version += 1


async def _broker_listener() -> None:
    global _layout, _state, _last_update_ts
    backoff = 2
    while True:
        try:
            log.info("connecting to broker at %s", CONFIG["broker_url"])
            async with websockets.connect(
                _ws_url(), ping_interval=20, ping_timeout=20
            ) as ws:
                log.info("connected")
                backoff = 2
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "full_state":
                        _layout = msg["layout"]
                        _state = msg["state"]
                    elif msg.get("type") == "update":
                        _state = msg["state"]
                    else:
                        continue
                    _last_update_ts = datetime.now()
                    _render_frame()
        except Exception as exc:  # noqa: BLE001
            log.warning("broker connection issue (%s); retrying in %ss", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.ensure_future(_broker_listener())


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


@app.get("/frame.png")
async def frame() -> Response:
    if _frame_bytes is None:
        from PIL import Image

        img = Image.new("RGB", (1360, 480), (255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")
    return Response(content=_frame_bytes, media_type="image/png")


@app.get("/api/version")
async def version() -> dict[str, int]:
    return {"version": _frame_version}


def _render_grid_overlay(cols: Optional[int] = None, rows: Optional[int] = None) -> bytes:
    """
    A transparent PNG the same size as the panel, with gridlines + (x,y)
    cell coordinates -- purely a layout-planning aid overlaid in the
    browser, never sent to the physical panel.

    cols/rows let you preview a DIFFERENT grid resolution than what's
    actually in the current layout.yaml -- e.g. "what would a 24x12 grid
    look like overlaid on my existing 12x6 widgets" -- without touching
    the real layout at all. Omit both to fall back to whatever grid.cols/
    grid.rows the currently-loaded layout defines (or the 12x6 default if
    nothing's connected yet), so it matches layout.yaml exactly by default.
    """
    from PIL import Image, ImageDraw

    width, height = 1360, 480
    if cols is None or rows is None:
        grid = (_layout or {}).get("grid", {"cols": 12, "rows": 6})
        cols = cols or max(int(grid.get("cols", 12)), 1)
        rows = rows or max(int(grid.get("rows", 6)), 1)
    cols = max(int(cols), 1)
    rows = max(int(rows), 1)
    cell_w = width / cols
    cell_h = height / rows

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    line_color = (0, 110, 255, 200)
    label_color = (0, 70, 200, 230)
    # Smaller cells (a finer grid) need a smaller label to still fit --
    # scale down with cell height, but keep it legible.
    font_size = max(7, min(11, int(cell_h * 0.28)))
    font = get_font(font_size, bold=False)

    for c in range(cols + 1):
        x = round(c * cell_w)
        draw.line([(x, 0), (x, height)], fill=line_color, width=1)
    for r in range(rows + 1):
        y = round(r * cell_h)
        draw.line([(0, y), (width, y)], fill=line_color, width=1)

    for gy in range(rows):
        for gx in range(cols):
            x = round(gx * cell_w) + 2
            y = round(gy * cell_h) + 1
            draw.text((x, y), f"{gx},{gy}", font=font, fill=label_color)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@app.get("/grid.png")
async def grid(cols: Optional[int] = None, rows: Optional[int] = None) -> Response:
    return Response(content=_render_grid_overlay(cols, rows), media_type="image/png")


@app.post("/api/demo")
async def load_demo_data() -> dict[str, str]:
    """
    Pushes sample values for every widget in layout.example.yaml straight to
    the broker, so you can see the whole thing render end-to-end before
    wiring up a real publisher.
    """
    demo_payload = {
        "header": {"title": "Home Dashboard", "subtitle": "Preview / demo data"},
        "weather_outdoor": {
            "condition": "sunny",
            "temp": 72,
            "temp_unit": "°F",
            "high": 78,
            "low": 61,
            "humidity": 41,
        },
        "metric_indoor_temp": {"label": "Indoor", "value": 70, "unit": "°F"},
        "metric_indoor_humidity": {"label": "Humidity", "value": 45, "unit": "%"},
        "list_status": {
            "items": [
                {"label": "Front Door", "value": "Locked"},
                {"label": "Garage", "value": "Closed"},
                {"label": "Alarm", "value": "Armed"},
            ]
        },
        "agenda_today": {
            "events": [
                {"time": "9:00 AM", "title": "Team standup"},
                {"time": "12:30 PM", "title": "Lunch with Sam"},
                {"time": "5:00 PM", "title": "Pick up kids"},
            ]
        },
        "ups_battery": {"label": "UPS Battery", "value": 100},
        "ups_load": {"label": "UPS Load", "value": 18, "unit": "%"},
        "ups_alert": {
            "active": True,
            "level": "critical",
            "text": "UPS on battery (ONBATT) -- 8 min left",
        },
    }
    # trust_env=False: don't let a stray system HTTP(S)/SOCKS proxy env var
    # intercept calls to our own configured broker on the LAN.
    async with httpx.AsyncClient(trust_env=False) as client:
        resp = await client.post(
            f"{CONFIG['broker_url'].rstrip('/')}/api/v1/widgets/bulk",
            json=demo_payload,
            headers={"Authorization": f"Bearer {CONFIG.get('token', '')}"},
            timeout=10,
        )
        resp.raise_for_status()
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=CONFIG["host"], port=CONFIG["port"])
