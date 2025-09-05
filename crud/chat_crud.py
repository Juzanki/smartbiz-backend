from __future__ import annotations
﻿from sqlalchemy.orm import Session
from backend.models.chat import ChatMessage
from backend.schemas import ChatCreate
from backend.schemas.user import UserOut
from typing import List

# ðŸ“¨ Tuma ujumbe mpya kwenye DB
def create_message(db: Session, chat: ChatCreate) -> ChatMessage:
    db_msg = ChatMessage(**chat.dict())
    db.add(db_msg)
    db.commit()
    db.refresh(db_msg)
    return db_msg

# ðŸ“¥ Pata jumbe zote za room fulani (kwa default limit ni 50)
def get_messages_by_room(db: Session, room_id: str, limit: int = 50) -> List[ChatMessage]:
    return (
        db.query(ChatMessage)
        .filter(ChatMessage.room_id == room_id)
        .order_by(ChatMessage.timestamp.desc())
        .limit(limit)
        .all()
    )


