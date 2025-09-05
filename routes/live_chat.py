# backend/routes/live_chat_ws.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import json
import time
from typing import Dict, Set, Optional

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException

router = APIRouter()

# ---- Tunables (adjust for your scale) ----
MAX_MSG_BYTES = 4096            # refuse overly large messages
RATE_LIMIT_PER_WINDOW = 30      # messages allowed
RATE_LIMIT_WINDOW_SEC = 10      # per this many seconds
HEARTBEAT_INTERVAL_SEC = 30     # app-level ping interval
SEND_TIMEOUT_SEC = 2            # drop slow consumers
ALLOWED_ORIGINS: Optional[Set[str]] = None  # e.g., {"https://your.app"}

UTC_NOW = lambda: int(time.time())  # epoch seconds


class ConnectionManager:
    """
    Simple, room-based WebSocket manager with:
      - room membership
      - safe broadcast (per-connection timeout)
      - presence counts
    """
    def __init__(self) -> None:
        self.rooms: Dict[str, Set[WebSocket]] = {}

    async def accept(self, ws: WebSocket) -> None:
        await ws.accept()

    def join(self, room_id: str, ws: WebSocket) -> None:
        self.rooms.setdefault(room_id, set()).add(ws)

    def leave(self, ws: WebSocket) -> None:
        # remove from all rooms safely
        for conns in self.rooms.values():
            if ws in conns:
                conns.discard(ws)

    def count(self, room_id: str) -> int:
        return len(self.rooms.get(room_id, set()))

    async def _safe_send_json(self, ws: WebSocket, payload: dict) -> bool:
        try:
            msg = json.dumps(payload, ensure_ascii=False)
            async with anyio.fail_after(SEND_TIMEOUT_SEC):
                await ws.send_text(msg)
            return True
        except Exception:
            return False

    async def broadcast_room(self, room_id: str, payload: dict, exclude: Optional[WebSocket] = None) -> None:
        conns = list(self.rooms.get(room_id, set()))
        if not conns:
            return
        # send sequentially with per-connection timeout; drop broken sockets
        to_drop: list[WebSocket] = []
        for ws in conns:
            if exclude is not None and ws is exclude:
                continue
            ok = await self._safe_send_json(ws, payload)
            if not ok:
                to_drop.append(ws)
        for ws in to_drop:
            self.leave(ws)


manager = ConnectionManager()


def _check_origin(ws: WebSocket) -> None:
    if not ALLOWED_ORIGINS:
        return
    origin = ws.headers.get("origin")
    if origin not in ALLOWED_ORIGINS:
        # Close immediately with a policy violation if you prefer
        raise HTTPException(status_code=403, detail="Origin not allowed")


class RateLimiter:
    """Fixed-window rate limiter per connection."""
    def __init__(self) -> None:
        self.win_start = UTC_NOW()
        self.count = 0

    def allow(self) -> bool:
        now = UTC_NOW()
        if now - self.win_start >= RATE_LIMIT_WINDOW_SEC:
            self.win_start = now
            self.count = 0
        self.count += 1
        return self.count <= RATE_LIMIT_PER_WINDOW


async def _heartbeat(ws: WebSocket, room_id: str, user_id: str):
    while True:
        await anyio.sleep(HEARTBEAT_INTERVAL_SEC)
        # Application-level ping (works across most WS servers)
        try:
            await manager._safe_send_json(ws, {
                "type": "ping",
                "room_id": room_id,
                "ts": UTC_NOW(),
            })
        except Exception:
            break


async def _handle_message(room_id: str, user_id: str, ws: WebSocket, raw: str, echo: bool) -> None:
    if len(raw.encode("utf-8")) > MAX_MSG_BYTES:
        await manager._safe_send_json(ws, {"type": "error", "error": "Message too large"})
        return

    # Try JSON; fall back to plain text
    try:
        data = json.loads(raw)
        mtype = data.get("type", "chat_message")
        content = data.get("message") or data.get("text") or ""
        extra = {k: v for k, v in data.items() if k not in {"type", "message", "text"}}
    except Exception:
        mtype = "chat_message"
        content = raw
        extra = {}

    if mtype == "ping":
        await manager._safe_send_json(ws, {"type": "pong", "ts": UTC_NOW()})
        return

    event = {
        "type": mtype,
        "room_id": room_id,
        "user_id": user_id,
        "message": content,
        "ts": UTC_NOW(),
        **extra,
    }
    await manager.broadcast_room(room_id, event, exclude=None if echo else ws)


@router.websocket("/ws/live/chat/{room_id}")
async def ws_live_chat(
    websocket: WebSocket,
    room_id: str,
    user_id: str = Query("anonymous", description="Client-provided user id (optional)"),
    echo: bool = Query(True, description="Echo the sender's message back to them"),
):
    """
    Room-based live chat:
      - Path param `room_id` selects the room
      - Query `user_id` is optional (supply your authenticated user id)
      - Query `echo` controls whether the sender also receives their own broadcast
      - JSON envelope supported: {"type":"chat_message","message":"hi","...extra"}
    """
    _check_origin(websocket)
    await manager.accept(websocket)
    manager.join(room_id, websocket)

    limiter = RateLimiter()

    # Notify presence
    await manager.broadcast_room(room_id, {
        "type": "user_joined",
        "room_id": room_id,
        "user_id": user_id,
        "count": manager.count(room_id),
        "ts": UTC_NOW(),
    })

    async with anyio.create_task_group() as tg:
        # Heartbeat task
        tg.start_soon(_heartbeat, websocket, room_id, user_id)

        try:
            while True:
                raw = await websocket.receive_text()
                if not limiter.allow():
                    await manager._safe_send_json(websocket, {"type": "error", "error": "Rate limited"})
                    continue
                await _handle_message(room_id, user_id, websocket, raw, echo)
        except WebSocketDisconnect:
            # Normal closure
            pass
        except Exception:
            # Abnormal closure or server error
            pass
        finally:
            manager.leave(websocket)
            await manager.broadcast_room(room_id, {
                "type": "user_left",
                "room_id": room_id,
                "user_id": user_id,
                "count": manager.count(room_id),
                "ts": UTC_NOW(),
            })
            tg.cancel_scope.cancel()


# Backward-compatible global room endpoint
@router.websocket("/ws/live/chat")
async def ws_live_chat_global(
    websocket: WebSocket,
    user_id: str = Query("anonymous"),
    echo: bool = Query(True),
):
    await ws_live_chat(websocket, room_id="global", user_id=user_id, echo=echo)
