"""
Broker: the single source of truth the Pi display talks to.

Any number of "publishers" (Home Assistant, a cron job, a phone shortcut,
whatever) push widget data here over plain HTTP. Any number of "clients"
(the Pi, the local preview, a second Pi somewhere else) connect over
WebSocket and get the full state on connect plus a push every time
something changes. Nothing here is Home Assistant-specific -- it's just a
generic key/value store (keyed by widget id) with pub/sub on top, plus the
layout definition so clients don't need their own copy.

Run directly for local dev:
    python app.py
Run in production:
    uvicorn app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("BROKER_CONFIG", BASE_DIR / "config.yaml"))


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "host": "0.0.0.0",
        "port": 8080,
        "token": "",
        "state_file": str(BASE_DIR / "data" / "state.json"),
        "layout_file": str(
            BASE_DIR.parent / "shared" / "dashboard_render" / "layout.example.yaml"
        ),
    }
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg.update(yaml.safe_load(f) or {})
    # Env var always wins so the token never has to live in a committed file.
    env_token = os.environ.get("DASHBOARD_TOKEN")
    if env_token:
        cfg["token"] = env_token
    return cfg


CONFIG = load_config()
STATE_FILE = Path(CONFIG["state_file"])
LAYOUT_FILE = Path(CONFIG["layout_file"])
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

if not CONFIG["token"]:
    print(
        "WARNING: no auth token configured (set DASHBOARD_TOKEN or 'token:' in "
        "config.yaml). The broker is running WIDE OPEN -- anyone who can reach "
        "it can read/write your dashboard. Fine for a quick local test, not "
        "for anything reachable from the internet."
    )


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def load_layout() -> dict[str, Any]:
    with open(LAYOUT_FILE) as f:
        return yaml.safe_load(f)


state_lock = asyncio.Lock()
dashboard_state: dict[str, Any] = load_state()

app = FastAPI(title="E-Ink Dashboard Broker")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


def check_token(provided: Optional[str]) -> None:
    if CONFIG["token"] and provided != CONFIG["token"]:
        raise HTTPException(status_code=401, detail="invalid or missing token")


def _bearer(authorization: Optional[str]) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:]
    return authorization


class WidgetUpdate(BaseModel):
    data: dict[str, Any]


@app.get("/api/v1/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "connected_clients": len(manager.active)}


@app.get("/api/v1/layout")
async def get_layout() -> dict[str, Any]:
    return load_layout()


@app.get("/api/v1/state")
async def get_state() -> dict[str, Any]:
    return dashboard_state


# NOTE: this must be declared BEFORE the "/widgets/{widget_id}" route below --
# FastAPI/Starlette match routes in declaration order, and a parameterized
# path declared first would otherwise swallow "bulk" as a widget_id.
@app.post("/api/v1/widgets/bulk")
async def update_widgets_bulk(
    payload: dict[str, dict[str, Any]],
    authorization: Optional[str] = Header(default=None),
) -> dict[str, str]:
    check_token(_bearer(authorization))
    async with state_lock:
        dashboard_state.update(payload)
        save_state(dashboard_state)
    await manager.broadcast(
        {"type": "update", "widget_ids": list(payload.keys()), "state": dashboard_state}
    )
    return {"status": "ok"}


@app.post("/api/v1/widgets/{widget_id}")
async def update_widget(
    widget_id: str,
    update: WidgetUpdate,
    authorization: Optional[str] = Header(default=None),
) -> dict[str, str]:
    check_token(_bearer(authorization))
    async with state_lock:
        dashboard_state[widget_id] = update.data
        save_state(dashboard_state)
    await manager.broadcast(
        {"type": "update", "widget_id": widget_id, "state": dashboard_state}
    )
    return {"status": "ok"}


@app.delete("/api/v1/widgets/{widget_id}")
async def delete_widget(
    widget_id: str, authorization: Optional[str] = Header(default=None)
) -> dict[str, str]:
    check_token(_bearer(authorization))
    async with state_lock:
        dashboard_state.pop(widget_id, None)
        save_state(dashboard_state)
    await manager.broadcast(
        {"type": "update", "widget_id": widget_id, "state": dashboard_state}
    )
    return {"status": "ok"}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: Optional[str] = None) -> None:
    try:
        check_token(token)
    except HTTPException:
        await websocket.close(code=4401)
        return

    await manager.connect(websocket)
    try:
        await websocket.send_json(
            {"type": "full_state", "layout": load_layout(), "state": dashboard_state}
        )
        while True:
            # Clients don't need to send anything, but reading keeps the
            # connection open and lets us notice disconnects promptly.
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=CONFIG["host"], port=CONFIG["port"])
