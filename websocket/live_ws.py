from fastapi import WebSocket, WebSocketDisconnect, APIRouter
from backend.websocket.live_ws_manager import live_room_manager

router = APIRouter()

@router.websocket("/ws/live/{stream_id}/{user_id}")
async def live_websocket(websocket: WebSocket, stream_id: int, user_id: int):
    await live_room_manager.connect(stream_id, websocket)
    
    # Notify others user joined
    await live_room_manager.broadcast(stream_id, {
        "event": "user_joined",
        "user_id": user_id
    })

    try:
        while True:
            await websocket.receive_text()  # Keep connection alive
    except WebSocketDisconnect:
        live_room_manager.disconnect(stream_id, websocket)

        # Notify others user left
        await live_room_manager.broadcast(stream_id, {
            "event": "user_left",
            "user_id": user_id
        })
