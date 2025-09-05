from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from typing import List
from backend.models.guests import Guest

# --- Add Guest to Livestream ---
def add_guest(db: Session, room_id: str, user_id: int) -> Guest:
    guest = Guest(room_id=room_id, user_id=user_id)
    db.add(guest)
    db.commit()
    db.refresh(guest)
    return guest

# --- Remove Guest ---
def remove_guest(db: Session, room_id: str, user_id: int) -> bool:
    guest = db.query(Guest).filter(
        Guest.room_id == room_id,
        Guest.user_id == user_id
    ).first()
    if guest:
        db.delete(guest)
        db.commit()
        return True
    return False

# --- Get Guests for a Stream ---
def get_stream_guests(db: Session, room_id: str) -> List[Guest]:
    return db.query(Guest).filter(
        Guest.room_id == room_id
    ).all()

