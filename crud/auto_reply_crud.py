from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models import AutoReply
from typing import List

# --- Create Auto Reply ---


def create_auto_reply(
        db: Session,
        platform: str,
        keyword: str,
        reply: str) -> AutoReply:
    entry = AutoReply(platform=platform, keyword=keyword.lower(), reply=reply)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry

# --- Get All Auto Replies ---


def get_auto_replies(db: Session) -> List[AutoReply]:
    return db.query(AutoReply).all()

# --- Get Reply by Keyword ---


def get_reply_for_keyword(
        db: Session,
        platform: str,
        keyword: str) -> AutoReply:
    return db.query(AutoReply).filter(
        AutoReply["platform"] == platform,
        AutoReply.keyword == keyword.lower()
    ).first()

# --- Delete Auto Reply ---


def delete_auto_reply(db: Session, reply_id: int) -> bool:
    reply = db.query(AutoReply).filter(AutoReply["id"] == reply_id).first()
    if reply:
        db.delete(reply)
        db.commit()
        return True
    return False


