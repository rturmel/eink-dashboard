"""
Home Assistant publisher.

Connects to Home Assistant's native WebSocket API, subscribes to
state_changed events, and -- using the mapping in entities.yaml -- pushes
only the specific fields you've chosen to the broker. Home Assistant never
talks to the Pi directly, and the Pi/broker never need an HA token: this
process is the only thing that needs to know your HA URL and long-lived
access token.

You can run this anywhere with network access to both HA and the broker
(same machine as HA, a different Pi, a container, etc.) -- it doesn't need
to be near the display.

Get a long-lived access token: HA > your profile (bottom left) > Security
tab > "Long-lived access tokens" > Create Token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

import httpx
import websockets
import yaml

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from mapping import build_widget_payload, referenced_entities  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s publisher_ha: %(message)s"
)
log = logging.getLogger("publisher_ha")


def load_config(path: Path) -> dict[str, Any]:
    with open(path) as f:
        cfg: dict[str, Any] = yaml.safe_load(f) or {}
    if os.environ.get("HA_TOKEN"):
        cfg["ha_token"] = os.environ["HA_TOKEN"]
    if os.environ.get("HA_URL"):
        cfg["ha_url"] = os.environ["HA_URL"]
    if os.environ.get("BROKER_URL"):
        cfg["broker_url"] = os.environ["BROKER_URL"]
    if os.environ.get("DASHBOARD_TOKEN"):
        cfg["broker_token"] = os.environ["DASHBOARD_TOKEN"]
    return cfg


def load_entities(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return (yaml.safe_load(f) or {}).get("widgets", {})


def _ha_ws_url(ha_url: str) -> str:
    base = ha_url.rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    if not ws_base.startswith(("ws://", "wss://")):
        ws_base = f"ws://{ws_base}"
    return f"{ws_base}/api/websocket"


class HAPublisher:
    def __init__(self, cfg: dict[str, Any], widgets_cfg: dict[str, Any]):
        self.cfg = cfg
        self.widgets_cfg = widgets_cfg
        self.entity_states: dict[str, dict[str, Any]] = {}
        self._widget_entities = {
            wid: referenced_entities(wcfg) for wid, wcfg in widgets_cfg.items()
        }
        self._dirty_widgets: set[str] = set()
        self._flush_task: Optional[asyncio.Task] = None
        self._msg_id = 1

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def run(self) -> None:
        backoff = 2
        while True:
            try:
                await self._connect_and_listen()
                backoff = 2
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                log.warning("HA connection lost (%s); retrying in %ss", exc, backoff)
            except Exception:
                log.exception("unexpected error talking to Home Assistant")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _connect_and_listen(self) -> None:
        url = _ha_ws_url(self.cfg["ha_url"])
        log.info("connecting to Home Assistant at %s", url)
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            await self._authenticate(ws)
            await self._prime_initial_states(ws)
            await self._subscribe_state_changed(ws)
            log.info("subscribed to state_changed events")

            # Push everything once up front so the dashboard isn't blank
            # until the first entity happens to change.
            self._dirty_widgets.update(self.widgets_cfg.keys())
            await self._flush()

            async for raw in ws:
                msg = json.loads(raw)
                self._handle_event(msg)

    async def _authenticate(self, ws) -> None:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected HA handshake: {hello}")
        await ws.send(json.dumps({"type": "auth", "access_token": self.cfg["ha_token"]}))
        result = json.loads(await ws.recv())
        if result.get("type") != "auth_ok":
            raise RuntimeError(f"HA auth failed: {result}")
        log.info("authenticated with Home Assistant")

    async def _prime_initial_states(self, ws) -> None:
        msg_id = self._next_id()
        await ws.send(json.dumps({"id": msg_id, "type": "get_states"}))
        while True:
            result = json.loads(await ws.recv())
            if result.get("id") == msg_id:
                for state in result.get("result", []):
                    self.entity_states[state["entity_id"]] = state
                log.info("loaded initial state for %d entities", len(self.entity_states))
                return

    async def _subscribe_state_changed(self, ws) -> None:
        msg_id = self._next_id()
        await ws.send(
            json.dumps({"id": msg_id, "type": "subscribe_events", "event_type": "state_changed"})
        )
        self._ws = ws  # stash for use in _handle_event's flush scheduling

    def _handle_event(self, msg: dict[str, Any]) -> None:
        if msg.get("type") != "event":
            return
        event = msg.get("event", {})
        if event.get("event_type") != "state_changed":
            return
        data = event.get("data", {})
        entity_id = data.get("entity_id")
        new_state = data.get("new_state")
        if not entity_id or new_state is None:
            return
        self.entity_states[entity_id] = new_state

        for widget_id, entities in self._widget_entities.items():
            if entity_id in entities:
                self._dirty_widgets.add(widget_id)

        if self._dirty_widgets and (self._flush_task is None or self._flush_task.done()):
            self._flush_task = asyncio.ensure_future(self._debounced_flush())

    async def _debounced_flush(self) -> None:
        # Coalesce a burst of entity changes (common right after HA restarts
        # or during an automation run) into one broker push.
        await asyncio.sleep(float(self.cfg.get("debounce_seconds", 2)))
        await self._flush()

    async def _flush(self) -> None:
        if not self._dirty_widgets:
            return
        payload = {}
        for widget_id in self._dirty_widgets:
            widget_cfg = self.widgets_cfg[widget_id]
            payload[widget_id] = build_widget_payload(widget_cfg, self.entity_states)
        self._dirty_widgets.clear()

        broker_url = self.cfg["broker_url"].rstrip("/")
        try:
            # trust_env=False: don't let a stray system HTTP(S)/SOCKS proxy
            # env var intercept calls to our own configured broker.
            async with httpx.AsyncClient(trust_env=False) as client:
                resp = await client.post(
                    f"{broker_url}/api/v1/widgets/bulk",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.cfg.get('broker_token', '')}"},
                    timeout=10,
                )
                resp.raise_for_status()
            log.info("pushed %d widget(s) to broker: %s", len(payload), list(payload))
        except Exception:
            log.exception("failed to push to broker; will retry on next change")
            self._dirty_widgets.update(payload.keys())  # retry these next time


async def main() -> None:
    parser = argparse.ArgumentParser(description="Home Assistant -> broker publisher")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--entities", default=str(BASE_DIR / "entities.yaml"))
    args = parser.parse_args()

    cfg_path, entities_path = Path(args.config), Path(args.entities)
    if not cfg_path.exists() or not entities_path.exists():
        log.error(
            "missing config: copy config.example.yaml -> config.yaml and "
            "entities.example.yaml -> entities.yaml, then edit both"
        )
        sys.exit(1)

    cfg = load_config(cfg_path)
    widgets_cfg = load_entities(entities_path)
    publisher = HAPublisher(cfg, widgets_cfg)
    await publisher.run()


if __name__ == "__main__":
    asyncio.run(main())
