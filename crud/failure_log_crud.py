from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.task_failure_log import TaskFailureLog
from backend.schemas.task_failure_log import TaskFailureLogBase
from datetime import datetime

def log_failure(db: Session, task_id: int, error_message: str):
    log = TaskFailureLog(
        task_id=task_id,
        error_message=error_message,
        timestamp=datetime.utcnow()
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log

