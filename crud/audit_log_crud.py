from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.audit_log import AuditLog
from typing import List
from datetime import datetime

# --- Log User Action ---


def log_action(
        db: Session,
        user_id: int,
        action: str,
        target: str) -> AuditLog:
    entry = AuditLog(
        user_id=user_id,
        action=action,
        target=target,
        timestamp=datetime.utcnow()
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry

# --- Get User Audit Logs ---


def get_user_logs(db: Session, user_id: int) -> List[AuditLog]:
    return db.query(AuditLog).filter(
        AuditLog.user_id == user_id).order_by(
        AuditLog.timestamp.desc()).all()

# --- Get All Logs ---


def get_all_logs(db: Session) -> List[AuditLog]:
    return db.query(AuditLog).order_by(AuditLog.timestamp.desc()).all()


