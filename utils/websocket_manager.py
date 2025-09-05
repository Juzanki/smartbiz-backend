# backend/utils/websocket_manager.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from typing import Dict, Set, Iterable, Optional, Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

class WebSocketManager:
    """
    Lightweight, concurrency-safe WS hub keyed by stream_id (int).

    Features:
    - connect/disconnect with lock
    - safe broadcast with auto-cleanup of dead sockets
    - send_personal, broadcast, broadcast_many, broadcast_all
    - count helpers, close_stream
    - optional heartbeat ping()
    """

    def __init__(self) -> None:
        self._active: Dict[int, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    # --------------------------
    # Internal helpers
    # --------------------------
    def _get_set_nolock(self, stream_id: int) -> Set[WebSocket]:
        return self._active.setdefault(stream_id, set())

    async def _safe_send_json(self, ws: WebSocket, message: Any) -> bool:
        """Send JSON; return False if socket is closed/broken."""
        try:
            if ws.application_state == WebSocketState.CONNECTED:
                await ws.send_json(message)
                return True
        except Exception:
            # swallow and let caller cleanup
            pass
        return False

    async def _remove_dead_nolock(self, stream_id: int, dead: Iterable[WebSocket]) -> None:
        s = self._active.get(stream_id)
        if not s:
            return
        for ws in dead:
            s.discard(ws)
        if not s:
            self._active.pop(stream_id, None)

    # --------------------------
    # Public API
    # --------------------------
    async def connect(self, stream_id: int, ws: WebSocket) -> None:
        """Accept and register a socket for a stream."""
        await ws.accept()
        async with self._lock:
            self._get_set_nolock(stream_id).add(ws)

    async def disconnect(self, stream_id: int, ws: WebSocket) -> None:
        """Remove socket from a stream."""
        async with self._lock:
            s = self._active.get(stream_id)
            if not s:
                return
            s.discard(ws)
            if not s:
                self._active.pop(stream_id, None)

    async def send_personal(self, ws: WebSocket, message: Any) -> None:
        """Send to one socket (no registration needed)."""
        await self._safe_send_json(ws, message)

    async def broadcast(self, stream_id: int, message: Any) -> int:
        """
        Send to all sockets in a stream.
        Returns the number of successful deliveries.
        """
        async with self._lock:
            sockets = set(self._active.get(stream_id, set()))
        if not sockets:
            return 0

        delivered = 0
        dead: Set[WebSocket] = set()
        for ws in sockets:
            ok = await self._safe_send_json(ws, message)
            if ok:
                delivered += 1
            else:
                dead.add(ws)

        if dead:
            async with self._lock:
                await self._remove_dead_nolock(stream_id, dead)
        return delivered

    async def broadcast_many(self, stream_ids: Iterable[int], message: Any) -> int:
        """Broadcast the same message to multiple streams; returns total deliveries."""
        total = 0
        for sid in set(stream_ids):
            total += await self.broadcast(sid, message)
        return total

    async def broadcast_all(self, message: Any) -> int:
        """Broadcast to every connected socket across all streams."""
        async with self._lock:
            stream_ids = list(self._active.keys())
        total = 0
        for sid in stream_ids:
            total += await self.broadcast(sid, message)
        return total

    # --------------------------
    # Maintenance / Insights
    # --------------------------
    async def count(self, stream_id: Optional[int] = None) -> int:
        """Count sockets for a stream or all."""
        async with self._lock:
            if stream_id is None:
                return sum(len(s) for s in self._active.values())
            return len(self._active.get(stream_id, set()))

    async def close_stream(self, stream_id: int, code: int = 1000, reason: str = "") -> int:
        """Close all sockets in a stream and remove the group."""
        async with self._lock:
            sockets = self._active.pop(stream_id, set())
        closed = 0
        for ws in sockets:
            try:
                await ws.close(code=code)
                closed += 1
            except Exception:
                pass
        return closed

    async def ping(self, stream_id: Optional[int] = None, payload: Any = {"type": "ping"}) -> int:
        """
        Optional heartbeat; sends a small ping and prunes dead sockets.
        Returns deliveries.
        """
        if stream_id is None:
            return await self.broadcast_all(payload)
        return await self.broadcast(stream_id, payload)
