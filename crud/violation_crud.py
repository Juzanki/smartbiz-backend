from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models import ViolationReport
from datetime import datetime
from typing import List

# --- Log Violation ---


def log_violation(
        db: Session,
        stream_id: int,
        user_id: int,
        reason: str) -> ViolationReport:
    report = ViolationReport(
        stream_id=stream_id,
        user_id=user_id,
        reason=reason,
        detected_at=datetime.utcnow()
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report

# --- Get All Violations ---


def get_all_violations(db: Session) -> List[ViolationReport]:
    return db.query(ViolationReport).order_by(
        ViolationReport.detected_at.desc()).all()

# --- Get Violations by User ---


def get_user_violations(db: Session, user_id: int) -> List[ViolationReport]:
    return db.query(ViolationReport).filter(
        ViolationReport.user_id == user_id).all()


