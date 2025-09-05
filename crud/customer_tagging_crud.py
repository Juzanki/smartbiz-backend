from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models import CustomerTag
from typing import List

# --- Add Tag to Customer ---


def add_customer_tag(db: Session, user_id: int, tag: str) -> CustomerTag:
    tag_entry = CustomerTag(user_id=user_id, tag=tag)
    db.add(tag_entry)
    db.commit()
    db.refresh(tag_entry)
    return tag_entry

# --- Get All Tags for Customer ---


def get_customer_tags(db: Session, user_id: int) -> List[CustomerTag]:
    return db.query(CustomerTag).filter(CustomerTag.user_id == user_id).all()

# --- Remove Tag ---


def remove_customer_tag(db: Session, tag_id: int) -> bool:
    tag = db.query(CustomerTag).filter(CustomerTag["id"] == tag_id).first()
    if tag:
        db.delete(tag)
        db.commit()
        return True
    return False


