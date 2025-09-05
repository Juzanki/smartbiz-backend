from __future__ import annotations
# backend/routes/scheduled_tasks.py
import hashlib
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter, Depends, HTTPException, Header, Query, Response, status, Body, Path
)
from sqlalchemy.orm import Session
from sqlalchemy import and_

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User as UserModel  # for type hints

# ---------- Schemas ----------
with suppress(Exception):
    from backend.schemas.scheduled_task import ScheduledTaskCreate, ScheduledTaskOut, ScheduledTaskUpdate  # type: ignore
with suppress(Exception):
    from backend.schemas.task_failure_log import TaskFailureLogOut  # type: ignore

# Fallbacks endapo schemas hazijapakiwa bado
if "ScheduledTaskCreate" not in globals():
    from pydantic import BaseModel, Field
    class ScheduledTaskCreate(BaseModel):
        user_id: int
        type: str = Field(..., min_length=1, max_length=64)
        content: str = Field(..., min_length=1, max_length=4000)
        scheduled_time: datetime

    class ScheduledTaskUpdate(BaseModel):
        type: Optional[str] = None
        content: Optional[str] = None
        scheduled_time: Optional[datetime] = None
        status: Optional[str] = None  # e.g. pending/sent/canceled/failed

    class ScheduledTaskOut(ScheduledTaskCreate):
        id: int
        status: str
        retry_count: Optional[int] = 0
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None
        canceled_at: Optional[datetime] = None
        class Config: orm_mode = True
        model_config = {"from_attributes": True}

if "TaskFailureLogOut" not in globals():
    from pydantic import BaseModel
    class TaskFailureLogOut(BaseModel):
        id: int
        task_id: int
        error: str
        occurred_at: datetime
        class Config: orm_mode = True
        model_config = {"from_attributes": True}

# ---------- Models ----------
ST = None
TFL = None
with suppress(Exception):
    from backend.models.scheduled_task import ScheduledTask as ST  # type: ignore
with suppress(Exception):
    from backend.models.task_failure_log import TaskFailureLog as TFL  # type: ignore

router = APIRouter(prefix="/scheduled-tasks", tags=["Scheduled Tasks"])

# ---------- Utils ----------
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def _serialize_task(obj: Any) -> ScheduledTaskOut:
    if hasattr(ScheduledTaskOut, "model_validate"):
        return ScheduledTaskOut.model_validate(obj, from_attributes=True)  # pydantic v2
    return ScheduledTaskOut.model_validate(obj)  # pydantic v1

def _serialize_logs(rows: List[Any]) -> List[TaskFailureLogOut]:
    out: List[TaskFailureLogOut] = []
    for r in rows:
        if hasattr(TaskFailureLogOut, "model_validate"):
            out.append(TaskFailureLogOut.model_validate(r, from_attributes=True))
        else:
            out.append(TaskFailureLogOut.model_validate(r))
    return out

def _etag(rows: List[Any], extra: str = "") -> str:
    if not rows:
        seed = f"0|{extra}"
    else:
        last = max(
            getattr(r, "updated_at", None)
            or getattr(r, "created_at", None)
            or _utc_now()
            for r in rows
        )
        seed = f"{len(rows)}|{last.isoformat()}|{extra}"
    return 'W/"' + hashlib.sha256(seed.encode()).hexdigest()[:16] + '"'

# ---------- CREATE ----------
@router.post(
    "",
    response_model=ScheduledTaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a scheduled task (with validation + optional idempotency)"
)
def create_task(
    task: ScheduledTaskCreate,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    min_lead_seconds: int = Query(10, ge=0, le=3600,
                                  description="Minimum seconds from now to scheduled_time"),
):
    if not ST:
        raise HTTPException(status_code=500, detail="ScheduledTask model not available")

    # Auth: user can create for self; admin can create for others
    if task.user_id != current_user.id and getattr(current_user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="Unauthorized to create for other users")

    # Time validation
    st = _ensure_utc(task.scheduled_time)
    if st < _utc_now() + timedelta(seconds=min_lead_seconds):
        raise HTTPException(status_code=400, detail="scheduled_time must be in the future")

    # Optional idempotency via DB column (if exists): UNIQUE(user_id, idempotency_key)
    if idempotency_key and hasattr(ST, "idempotency_key"):
        existing = (
            db.query(ST)
            .filter(ST.user_id == task.user_id, ST.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            return _serialize_task(existing)

    row = ST(
        user_id=task.user_id,
        type=task.type.strip(),
        content=task.content.strip(),
        scheduled_time=st,
    )
    if hasattr(row, "status"):
        row.status = "pending"
    if hasattr(row, "retry_count") and getattr(row, "retry_count") is None:
        row.retry_count = 0
    if hasattr(row, "created_at"): row.created_at = _utc_now()
    if hasattr(row, "updated_at"): row.updated_at = _utc_now()
    if idempotency_key and hasattr(row, "idempotency_key"):
        row.idempotency_key = idempotency_key

    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_task(row)

# ---------- LIST (my tasks) ----------
@router.get(
    "",
    response_model=List[ScheduledTaskOut],
    summary="List my scheduled tasks (filters + pagination + ETag)"
)
def get_my_tasks(
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
    status_filter: str = Query("any", pattern="^(any|pending|sent|failed|canceled)$"),
    type_filter: Optional[str] = Query(None, min_length=1, max_length=64),
    since: Optional[datetime] = Query(None, description="UTC ISO start"),
    until: Optional[datetime] = Query(None, description="UTC ISO end"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    if not ST:
        raise HTTPException(status_code=500, detail="ScheduledTask model not available")

    q = db.query(ST).filter(ST.user_id == current_user.id)

    if status_filter != "any":
        if hasattr(ST, "status"):
            q = q.filter(ST.status == status_filter)
        else:
            # fallback to sent=True/False if status column doesn't exist
            if status_filter == "pending":
                q = q.filter(getattr(ST, "sent") == False)  # noqa: E712
            elif status_filter == "sent":
                q = q.filter(getattr(ST, "sent") == True)   # noqa: E712
            else:
                # for failed/canceled without columns, return empty
                q = q.filter(ST.id == -1)

    if type_filter:
        q = q.filter(ST.type == type_filter)

    if since:
        q = q.filter(ST.scheduled_time >= _ensure_utc(since))
    if until:
        q = q.filter(ST.scheduled_time <= _ensure_utc(until))

    order_col = getattr(ST, "scheduled_time", getattr(ST, "id"))
    q = q.order_by(order_col.asc() if order == "asc" else order_col.desc())

    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    tag = _etag(rows, extra=f"{current_user.id}|{status_filter}|{type_filter}|{since}|{until}|{limit}|{offset}|{order}")
    if if_none_match and if_none_match == tag:
        return Response(status_code=304)
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "public, max-age=10"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    return [_serialize_task(r) for r in rows]

# ---------- GET by ID ----------
@router.get(
    "/{task_id}",
    response_model=ScheduledTaskOut,
    summary="Get a scheduled task by ID"
)
def get_task(
    task_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if not ST:
        raise HTTPException(status_code=500, detail="ScheduledTask model not available")

    row = db.query(ST).filter(ST.id == task_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    if row.user_id != current_user.id and getattr(current_user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    return _serialize_task(row)

# ---------- UPDATE (PATCH) ----------
@router.patch(
    "/{task_id}",
    response_model=ScheduledTaskOut,
    summary="Update a scheduled task (only pending)"
)
def update_task(
    task_id: int,
    payload: ScheduledTaskUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if not ST:
        raise HTTPException(status_code=500, detail="ScheduledTask model not available")

    row = db.query(ST).filter(ST.id == task_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    if row.user_id != current_user.id and getattr(current_user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    # Ruhusu update kabla ya kutumwa
    already_sent = False
    if hasattr(row, "status"):
        already_sent = row.status == "sent"
    elif hasattr(row, "sent"):
        already_sent = bool(row.sent)
    if already_sent:
        raise HTTPException(status_code=409, detail="Task already sent")

    data = payload.dict(exclude_unset=True)
    if "type" in data and data["type"]:
        row.type = data["type"].strip()
    if "content" in data and data["content"]:
        row.content = data["content"].strip()
    if "scheduled_time" in data and data["scheduled_time"]:
        st = _ensure_utc(data["scheduled_time"])
        if st < _utc_now() + timedelta(seconds=5):
            raise HTTPException(status_code=400, detail="scheduled_time must be in the future")
        row.scheduled_time = st
    if "status" in data and hasattr(row, "status"):
        # ruhusu admin tu kubadilisha status moja kwa moja
        if getattr(current_user, "role", "") != "admin":
            raise HTTPException(status_code=403, detail="Only admin can set status")
        row.status = data["status"]

    if hasattr(row, "updated_at"):
        row.updated_at = _utc_now()

    db.commit()
    db.refresh(row)
    return _serialize_task(row)

# ---------- CANCEL ----------
@router.post(
    "/{task_id}/cancel",
    response_model=dict,
    summary="Cancel a scheduled task (if pending)"
)
def cancel_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if not ST:
        raise HTTPException(status_code=500, detail="ScheduledTask model not available")

    row = db.query(ST).filter(ST.id == task_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    if row.user_id != current_user.id and getattr(current_user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    already_sent = False
    if hasattr(row, "status"):
        already_sent = row.status == "sent"
    elif hasattr(row, "sent"):
        already_sent = bool(row.sent)
    if already_sent:
        raise HTTPException(status_code=409, detail="Task already sent")

    if hasattr(row, "status"):
        row.status = "canceled"
    if hasattr(row, "canceled_at"):
        row.canceled_at = _utc_now()
    elif hasattr(row, "sent"):
        row.sent = True  # poor-man cancel

    if hasattr(row, "updated_at"):
        row.updated_at = _utc_now()

    db.commit()
    return {"detail": "canceled"}

# ---------- FAILURES (with access control) ----------
@router.get(
    "/{task_id}/failures",
    response_model=List[TaskFailureLogOut],
    summary="List failure logs for a task"
)
def get_task_failures(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user)
):
    if not (ST and TFL):
        raise HTTPException(status_code=500, detail="Models not available")

    task = db.query(ST).filter(ST.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Ruhusa: owner au admin
    if task.user_id != current_user.id and getattr(current_user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    logs = db.query(TFL).filter(TFL.task_id == task_id).order_by(TFL.occurred_at.desc()).all()
    return _serialize_logs(logs)

