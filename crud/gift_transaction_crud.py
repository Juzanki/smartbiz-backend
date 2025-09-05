from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.gift_transaction import GiftTransaction
from datetime import datetime
from typing import List

# --- Send Gift ---


def send_gift(
        db: Session,
        sender_id: int,
        receiver_id: int,
        gift_id: int,
        stream_id: int,
        quantity: int = 1) -> GiftTransaction:
    tx = GiftTransaction(
        sender_id=sender_id,
        receiver_id=receiver_id,
        gift_id=gift_id,
        stream_id=stream_id,
        quantity=quantity,
        sent_at=datetime.utcnow()
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx

# --- Get Gifts Sent in Stream ---


def get_stream_gifts(db: Session, stream_id: int) -> List[GiftTransaction]:
    return db.query(GiftTransaction).filter(
        GiftTransaction.stream_id == stream_id).order_by(
        GiftTransaction.sent_at.asc()).all()

# --- Get User Gift History ---


def get_user_gift_history(db: Session, user_id: int) -> List[GiftTransaction]:
    return db.query(GiftTransaction).filter(
        GiftTransaction.sender_id == user_id).order_by(
        GiftTransaction.sent_at.desc()).all()


