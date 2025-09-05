from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from backend.websocket.manager import ConnectionManager

router = APIRouter()
manager = ConnectionManager()

@router.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await manager.connect(user_id, websocket)
    try:
        while True:
            await websocket.receive_text()  # Ping-pong mechanism
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
