from __future__ import annotations
﻿from backend.schemas.user import UserOut
# backend/crud/host_crud.py
from sqlalchemy.orm import Session
from backend.schemas.host_schemas import CoHostInviteCreate, CoHostInviteUpdate

def create_invite(db: Session, sender_id: int, invite: CoHostInviteCreate):
    db_invite = CoHostInvitation(
        stream_id=invite.stream_id,
        sender_id=sender_id,
        receiver_id=invite.receiver_id
    )
    db.add(db_invite)
    db.commit()
    db.refresh(db_invite)
    return db_invite

def update_invite_status(db: Session, invite_id: int, status: str):
    db_invite = db.query(CoHostInvitation).filter(CoHostInvitation.id == invite_id).first()
    if db_invite:
        db_invite.status = status
        db.commit()
    return db_invite

def get_stream_invites(db: Session, stream_id: int):
    return db.query(CoHostInvitation).filter(CoHostInvitation.stream_id == stream_id).all()

