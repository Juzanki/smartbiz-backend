from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models import SmartTask
from typing import List
from datetime import datetime

# --- Create Smart Task ---


def create_task(
        db: Session,
        user_id: int,
        title: str,
        description: str,
        execute_at: datetime) -> SmartTask:
    task = SmartTask(
        user_id=user_id,
        title=title,
        description=description,
        execute_at=execute_at,
        created_at=datetime.utcnow()
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task

# --- Get Pending Tasks ---


def get_pending_tasks(db: Session) -> List[SmartTask]:
    now = datetime.utcnow()
    return db.query(SmartTask).filter(
        SmartTask.executed == False,
        SmartTask.execute_at <= now).all()

# --- Mark Task as Executed ---


def mark_task_executed(db: Session, task_id: int):
    task = db.query(SmartTask).filter(SmartTask["id"] == task_id).first()
    if task:
        task.executed = True
        db.commit()
        db.refresh(task)
    return task


