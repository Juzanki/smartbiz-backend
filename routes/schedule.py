from __future__ import annotations
# backend/routes/schedule.py
import hashlib
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter, Depends, HTTPException, Header, Query, Response, status, Body, Path
)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import and_

from backend.db import get_db
from backend.auth import get_current_user

# --------- Schemas (tumia zako; hizi ni fallbacks endapo hazijapakiwa) ---------
with suppress(Exception):
    from backend.schemas import ScheduledMessageCreate as _SMCreate  # type: ignore
    from backend.schemas import ScheduledMessageOut as _SMOut        # type: ignore

if "_SMCreate" in globals():
    ScheduledMessageCreate = _SMCreate
else:
    class ScheduledMessageCreate(BaseModel):
        content: str = Field(..., min_length=1, max_length=4000)
        scheduled_time: datetime = Field(..., description="UTC ISO8601 e.g. 2025-08-21T10:00:00Z")
        channel: Optional[str] = Field(None, description="telegram|whatsapp|sms|email etc.")

if "_SMOut" in globals():
    ScheduledMessageOut = _SMOut
else:
    class ScheduledMessageOut(ScheduledMessageCreate):
        id: int
        user_id: int
        sent: bool = False
        status: Optional[str] = None
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None
        canceled_at: Optional[datetime] = None
        class Config: orm_mode = True
        model_config = {"from_attributes": True}

# --------- Model ---------
SM = None
with suppress(Exception):
    from backend.models.message import ScheduledMessage as SM

router = APIRouter(prefix="/schedule", tags=["Scheduled Promotions"])

# --------- Utils ---------
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _ensure_utc(dt: datetime) -> datetime:
    # Kawaida clients hutuma "Z"; kama sio aware, chukulia kama UTC
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def _etag_list(rows: List[Any], extra: str = "") -> str:
    if not rows:
        seed = f"0|{extra}"
    else:
        last = max(
            getattr(r, "updated_at", None) or getattr(r, "created_at", None) or _utc_now()
            for r in rows
        )
        seed = f"{len(rows)}|{last.isoformat()}|{extra}"
    return 'W/"' + hashlib.sha256(seed.encode()).hexdigest()[:16] + '"'

def _serialize(obj: Any) -> ScheduledMessageOut:
    if hasattr(ScheduledMessageOut, "model_validate"):  # pydantic v2
        return ScheduledMessageOut.model_validate(obj, from_attributes=True)
    return ScheduledMessageOut.model_validate(obj)            # pydantic v1

# --------- CREATE (idempotent + validate) ---------
@router.post(
    "",
    response_model=ScheduledMessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Panga ujumbe (idempotent, valid time)"
)
def schedule_message(
    payload: ScheduledMessageCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    min_lead_seconds: int = Query(15, ge=0, le=3600,
                                  description="Ndogo zaidi kabla ya muda wa kutuma (sekunde)"),
):
    if not SM:
        raise HTTPException(status_code=500, detail="ScheduledMessage model haijapatikana")

    # Validate time
    st = _ensure_utc(payload.scheduled_time)
    if st < _utc_now() + timedelta(seconds=min_lead_seconds):
        raise HTTPException(status_code=400, detail="scheduled_time must be in the future")

    # Idempotency (kama una column idempotency_key)
    if idempotency_key and hasattr(SM, "idempotency_key"):
        exists = (
            db.query(SM)
            .filter(SM.user_id == current_user.id, SM.idempotency_key == idempotency_key)
            .first()
        )
        if exists:
            return _serialize(exists)

    row = SM(
        user_id=getattr(current_user, "id"),
        content=payload.content.strip(),
        scheduled_time=st,
    )
    if hasattr(row, "channel") and getattr(payload, "channel", None):
        row.channel = payload.channel
    if hasattr(row, "status"):
        row.status = "pending"
    if hasattr(row, "created_at"): row.created_at = _utc_now()
    if hasattr(row, "updated_at"): row.updated_at = _utc_now()
    if idempotency_key and hasattr(row, "idempotency_key"):
        row.idempotency_key = idempotency_key

    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize(row)

# --------- LIST (filters + pagination + ETag) ---------
@router.get(
    "",
    response_model=List[ScheduledMessageOut],
    summary="Orodha ya ujumbe uliopangwa (filters + pagination + ETag)"
)
def get_my_scheduled_messages(
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    status_filter: str = Query("any", pattern="^(any|pending|sent|canceled)$"),
    since: Optional[datetime] = Query(None, description="ISO start time (UTC)"),
    until: Optional[datetime] = Query(None, description="ISO end time (UTC)"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    if not SM:
        raise HTTPException(status_code=500, detail="ScheduledMessage model haijapatikana")

    q = db.query(SM).filter(SM.user_id == current_user.id)

    # status filtering (jaribu kutumia fields zilizopo)
    if status_filter != "any":
        if status_filter == "pending":
            if hasattr(SM, "status"):
                q = q.filter(SM.status == "pending")
            else:
                q = q.filter(SM.sent == False)  # noqa: E712
        elif status_filter == "sent":
            if hasattr(SM, "status"):
                q = q.filter(SM.status == "sent")
            else:
                q = q.filter(SM.sent == True)   # noqa: E712
        elif status_filter == "canceled":
            if hasattr(SM, "status"):
                q = q.filter(SM.status == "canceled")
            elif hasattr(SM, "canceled_at"):
                q = q.filter(SM.canceled_at.isnot(None))
            else:
                # fallback: hakuna notion ya canceled
                q = q.filter(SM.id == -1)  # empty

    if since:
        q = q.filter(SM.scheduled_time >= _ensure_utc(since))
    if until:
        q = q.filter(SM.scheduled_time <= _ensure_utc(until))

    # order/paging
    order_col = getattr(SM, "scheduled_time", getattr(SM, "id"))
    q = q.order_by(order_col.asc() if order == "asc" else order_col.desc())

    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    tag = _etag_list(rows, extra=f"{current_user.id}|{status_filter}|{since}|{until}|{limit}|{offset}|{order}")
    if if_none_match and if_none_match == tag:
        return Response(status_code=304)
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "public, max-age=10"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    return [_serialize(r) for r in rows]

# --------- GET by id ---------
@router.get(
    "/{message_id}",
    response_model=ScheduledMessageOut,
    summary="Pata ujumbe uliopangwa kwa ID"
)
def get_one(
    message_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
    response: Response = None,  # type: ignore
):
    if not SM:
        raise HTTPException(status_code=500, detail="ScheduledMessage model haijapatikana")
    row = db.query(SM).filter(SM.id == message_id, SM.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    tag = _etag_list([row], extra=str(message_id))
    if if_none_match and if_none_match == tag:
        return Response(status_code=304)
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "public, max-age=20"
    return _serialize(row)

# --------- UPDATE (PATCH) ---------
class _UpdatePayload(BaseModel):
    content: Optional[str] = Field(None, min_length=1, max_length=4000)
    scheduled_time: Optional[datetime] = None
    channel: Optional[str] = None

@router.patch(
    "/{message_id}",
    response_model=ScheduledMessageOut,
    summary="Sasisha ujumbe (kabla haujatumwa)"
)
def update_scheduled_message(
    message_id: int,
    payload: _UpdatePayload = Body(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not SM:
        raise HTTPException(status_code=500, detail="ScheduledMessage model haijapatikana")
    row = db.query(SM).filter(SM.id == message_id, SM.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    # Ruhusu ku-update kama bado pending/haijasafirishwa
    already_sent = bool(getattr(row, "sent", False)) or (getattr(row, "status", "") == "sent")
    if already_sent:
        raise HTTPException(status_code=409, detail="Already sent")

    data = payload.dict(exclude_unset=True)
    if "content" in data and data["content"] is not None:
        row.content = data["content"].strip()
    if "channel" in data and hasattr(row, "channel"):
        row.channel = data["channel"]
    if "scheduled_time" in data and data["scheduled_time"] is not None:
        st = _ensure_utc(data["scheduled_time"])
        if st < _utc_now() + timedelta(seconds=5):
            raise HTTPException(status_code=400, detail="scheduled_time must be in the future")
        row.scheduled_time = st

    if hasattr(row, "updated_at"):
        row.updated_at = _utc_now()

    db.commit()
    db.refresh(row)
    return _serialize(row)

# --------- CANCEL ---------
@router.post(
    "/{message_id}/cancel",
    response_model=dict,
    summary="Ghairi ujumbe uliopangwa"
)
def cancel_scheduled_message(
    message_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not SM:
        raise HTTPException(status_code=500, detail="ScheduledMessage model haijapatikana")
    row = db.query(SM).filter(SM.id == message_id, SM.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    if bool(getattr(row, "sent", False)) or getattr(row, "status", "") == "sent":
        raise HTTPException(status_code=409, detail="Already sent")

    if hasattr(row, "status"):
        row.status = "canceled"
    if hasattr(row, "canceled_at"):
        row.canceled_at = _utc_now()
    elif hasattr(row, "sent"):  # fallback poor-man cancel
        row.sent = True

    if hasattr(row, "updated_at"):
        row.updated_at = _utc_now()

    db.commit()
    return {"detail": "canceled"}


