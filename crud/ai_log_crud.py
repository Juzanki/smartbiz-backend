from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models import AILog
from datetime import datetime
from typing import List

# --- Log AI Interaction ---


def log_ai_interaction(
        db: Session,
        user_id: int,
        tool: str,
        prompt: str,
        response: str) -> AILog:
    log = AILog(
        user_id=user_id,
        tool=tool,
        prompt=prompt,
        response=response,
        created_at=datetime.utcnow()
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log

# --- Get AI Logs for User ---


def get_user_ai_logs(db: Session, user_id: int) -> List[AILog]:
    return db.query(AILog).filter(AILog.user_id == user_id).order_by(AILog["created_at"]desc()).all()

# --- Get Logs by Tool Type ---


def get_logs_by_tool(db: Session, tool: str) -> List[AILog]:
    return db.query(AILog).filter(AILog.tool == tool).order_by(AILog["created_at"]desc()).all()


