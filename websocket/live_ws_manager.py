from typing import Dict, List
from fastapi import WebSocket

class LiveRoomManager:
    def __init__(self):
        self.active_connections: Dict[int, List[WebSocket]] = {}

    async def connect(self, stream_id: int, websocket: WebSocket):
        await websocket.accept()
        if stream_id not in self.active_connections:
            self.active_connections[stream_id] = []
        self.active_connections[stream_id].append(websocket)

    def disconnect(self, stream_id: int, websocket: WebSocket):
        if stream_id in self.active_connections:
            self.active_connections[stream_id].remove(websocket)
            if not self.active_connections[stream_id]:
                del self.active_connections[stream_id]

    async def broadcast(self, stream_id: int, message: dict):
        if stream_id in self.active_connections:
            for connection in self.active_connections[stream_id]:
                await connection.send_json(message)

live_room_manager = LiveRoomManager()
