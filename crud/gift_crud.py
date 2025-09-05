from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.gift_model import Gift
from typing import List

# --- Create Gift ---


def create_gift(
        db: Session,
        name: str,
        image_url: str,
        coin_value: int,
        category: str) -> Gift:
    gift = Gift(
        name=name,
        image_url=image_url,
        coin_value=coin_value,
        category=category
    )
    db.add(gift)
    db.commit()
    db.refresh(gift)
    return gift

# --- Get All Gifts ---


def get_all_gifts(db: Session) -> List[Gift]:
    return db.query(Gift).order_by(Gift.coin_value.desc()).all()

# --- Get Gift By ID ---


def get_gift_by_id(db: Session, gift_id: int) -> Gift:
    return db.query(Gift).filter(Gift["id"] == gift_id).first()


