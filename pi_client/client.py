"""
Pi-side dashboard client.

Connects to the broker's WebSocket, keeps a local copy of the layout +
state, and pushes a rendered frame to the physical e-Paper panel. Contains
no Home Assistant knowledge whatsoever -- it only knows how to talk to the
broker and how to draw whatever the layout+state describe. That's what
makes it possible to point the exact same client at a different broker
and have it "just work" anywhere with Wi-Fi.

Two things make this safe for the hardware:

1. Debouncing: rapid-fire updates from the broker get coalesced into a
   single physical refresh, never more often than
   `min_refresh_interval_seconds` (Waveshare recommends >=180s for
   multi-color panels).
2. Forced periodic refresh: even with zero updates, the panel is
   redrawn at least every `force_refresh_seconds` so it's never left
   un-refreshed longer than Waveshare's 24h recommendation (avoids
   ghosting/burn-in).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import websockets
import yaml

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent / "shared"))

from dashboard_render import render_dashboard  # noqa: E402

from epd_display import EPDDisplay  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s pi_client: %(message)s"
)
log = logging.getLogger("pi_client")


def load_config(path: Path) -> dict[str, Any]:
    with open(path) as f:
        cfg: dict[str, Any] = yaml.safe_load(f) or {}
    # Env vars win, so secrets/host don't have to live in a committed file
    # (handy when the Pi is provisioned via an image/script rather than
    # hand-edited).
    if os.environ.get("DASHBOARD_TOKEN"):
        cfg["token"] = os.environ["DASHBOARD_TOKEN"]
    if os.environ.get("BROKER_URL"):
        cfg["broker_url"] = os.environ["BROKER_URL"]
    return cfg


class DashboardClient:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.layout: Optional[dict[str, Any]] = None
        self.state: dict[str, Any] = {}
        self.display = EPDDisplay(
            dry_run=cfg.get("dry_run", False),
            output_path=cfg.get("dry_run_output", "./preview_frame.png"),
            rotate=int(cfg.get("rotate_degrees", 0)),
        )
        self._dirty = False
        self._last_refresh = 0.0
        self._pending_task: Optional[asyncio.Task] = None
        # When the broker last told us data actually changed -- distinct
        # from "time" in the header (which is the last *physical* refresh,
        # set below). Debouncing means those two can be far apart: this
        # tells you how fresh the underlying data is even when the panel
        # itself hasn't redrawn in a while.
        self._last_data_update: Optional[datetime] = None

    @property
    def ws_url(self) -> str:
        base = self.cfg["broker_url"].rstrip("/")
        ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
        if not ws_base.startswith(("ws://", "wss://")):
            ws_base = f"ws://{ws_base}"
        token = self.cfg.get("token", "")
        return f"{ws_base}/ws?token={token}"

    async def run(self) -> None:
        backoff = 2
        while True:
            try:
                await self._connect_and_listen()
                backoff = 2  # reset after a clean connection
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                log.warning("connection lost (%s); retrying in %ss", exc, backoff)
            except Exception:
                log.exception("unexpected error in connection loop")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _connect_and_listen(self) -> None:
        log.info("connecting to %s", self.cfg["broker_url"])
        async with websockets.connect(
            self.ws_url, ping_interval=20, ping_timeout=20
        ) as ws:
            log.info("connected")
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._handle_message(msg)

    def _handle_message(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == "full_state":
            self.layout = msg["layout"]
            self.state = msg["state"]
            log.info("received full state (%d widgets)", len(self.state))
        elif msg.get("type") == "update":
            self.state = msg["state"]
            log.info(
                "state updated (%s)", msg.get("widget_id") or msg.get("widget_ids")
            )
        else:
            return
        self._last_data_update = datetime.now()
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        self._dirty = True
        if self._pending_task is None or self._pending_task.done():
            self._pending_task = asyncio.ensure_future(self._debounced_refresh())

    async def _debounced_refresh(self) -> None:
        min_interval = float(self.cfg.get("min_refresh_interval_seconds", 180))
        elapsed = time.monotonic() - self._last_refresh
        wait = max(min_interval - elapsed, 0)
        if wait > 0:
            log.info("debouncing: next refresh in %.0fs", wait)
            await asyncio.sleep(wait)
        if not self._dirty:
            return
        self._dirty = False
        await self._render_and_push()

    async def _render_and_push(self) -> None:
        if not self.layout:
            log.warning("no layout received yet, skipping render")
            return
        state = dict(self.state)
        header = dict(state.get("header", {}))
        header.setdefault("time", datetime.now().strftime("%-I:%M %p"))
        if self._last_data_update is not None:
            header.setdefault(
                "subtitle",
                f"Data updated {self._last_data_update.strftime('%b %-d, %-I:%M %p')}",
            )
        state["header"] = header

        loop = asyncio.get_running_loop()
        image = await loop.run_in_executor(None, render_dashboard, self.layout, state)
        await loop.run_in_executor(None, self.display.show, image)
        self._last_refresh = time.monotonic()
        log.info("refreshed panel")

    async def force_refresh_loop(self) -> None:
        interval = float(self.cfg.get("force_refresh_seconds", 6 * 3600))
        while True:
            await asyncio.sleep(interval)
            if self.layout:
                log.info("forced periodic refresh (interval=%ss)", interval)
                await self._render_and_push()


async def main() -> None:
    parser = argparse.ArgumentParser(description="E-ink dashboard Pi client")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        log.error(
            "config file not found: %s (copy config.example.yaml to config.yaml "
            "and edit it first)",
            cfg_path,
        )
        sys.exit(1)

    cfg = load_config(cfg_path)
    client = DashboardClient(cfg)
    await asyncio.gather(client.run(), client.force_refresh_loop())


if __name__ == "__main__":
    asyncio.run(main())
