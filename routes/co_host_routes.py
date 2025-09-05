from __future__ import annotations
# backend/routes/cohosts.py
import os
import time
import hashlib
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Literal
from contextlib import suppress

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Path, Response, Header
)
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User

try:
    from backend.models.co_host import CoHost   # model yako
except Exception:
    raise RuntimeError("Model 'CoHost' haijapatikana. Tengeneza backend/models/co_host.py")

# ===== Schemas =====
try:
    from backend.schemas.co_host_schema import (
        CoHostCreate, CoHostOut, CoHostStatusUpdate
    )
except Exception:
    # fallbacks ndogo ili route ifanye kazi hata kama schema haijakamilika bado
    from pydantic import BaseModel, Field
    class CoHostCreate(BaseModel):
        stream_id: str
        invitee_id: int
        message: Optional[str] = Field(None, max_length=500)

    class CoHostStatusUpdate(BaseModel):
        status: Literal["accepted", "rejected", "canceled"]
        reason: Optional[str] = Field(None, max_length=300)

    class CoHostOut(BaseModel):
        id: int
        stream_id: str
        inviter_id: int
        invitee_id: int
        status: str
        message: Optional[str] = None
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None
        accepted_at: Optional[datetime] = None
        rejected_at: Optional[datetime] = None
        canceled_at: Optional[datetime] = None

router = APIRouter(prefix="/cohosts", tags=["Co-Hosts"])

# ===== Config & helpers =====
RATE_PER_MIN = int(os.getenv("COHOST_INVITE_RATE_PER_MIN", "30"))
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
ALLOWED_SORT = ("id", "created_at", "updated_at", "status")
ALLOWED_ORDER = ("asc", "desc")

_RATE: Dict[int, List[float]] = {}
_IDEMP: Dict[tuple[int, str], float] = {}
_IDEMP_TTL = 10 * 60  # s

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _rate_ok(inviter_id: int) -> None:
    now = time.time()
    q = _RATE.setdefault(inviter_id, [])
    while q and (now - q[0]) > 60.0:
        q.pop(0)
    if len(q) >= RATE_PER_MIN:
        raise HTTPException(status_code=429, detail="Too many invites this minute")
    q.append(now)

def _idempotency_check(uid: int, key: Optional[str]) -> None:
    if not key:
        return
    now = time.time()
    # safisha zilizopitwa
    stale = [(k_uid, k) for (k_uid, k), ts in _IDEMP.items() if now - ts > _IDEMP_TTL]
    for s in stale:
        _IDEMP.pop(s, None)
    token = (uid, key.strip())
    if token in _IDEMP:
        raise HTTPException(status_code=409, detail="Duplicate request (Idempotency-Key)")
    _IDEMP[token] = now

def _clamp_limit(v: Optional[int]) -> int:
    if not v:
        return DEFAULT_LIMIT
    return max(1, min(int(v), MAX_LIMIT))

def _order_by(model, sort_by: str, order: str):
    key = sort_by if sort_by in ALLOWED_SORT else "id"
    col = getattr(model, key, getattr(model, "id"))
    return col.asc() if order == "asc" else col.desc()

def _etag(row: Any) -> str:
    base = f"{getattr(row, 'id', '')}-{getattr(row, 'updated_at', '') or getattr(row, 'status', '')}"
    return 'W/"' + hashlib.sha256(str(base).encode("utf-8")).hexdigest()[:16] + '"'

def _apply_status_time(inv: Any, status_val: str) -> None:
    now = _utcnow()
    if status_val == "accepted" and hasattr(inv, "accepted_at"):
        inv.accepted_at = now
    if status_val == "rejected" and hasattr(inv, "rejected_at"):
        inv.rejected_at = now
    if status_val == "canceled" and hasattr(inv, "canceled_at"):
        inv.canceled_at = now
    if hasattr(inv, "updated_at"):
        inv.updated_at = now

def _serialize(obj: Any) -> CoHostOut:
    # weka uoanifu wa pydantic v1/v2
    if hasattr(CoHostOut, "model_validate"):
        return CoHostOut.model_validate(obj, from_attributes=True)  # v2
    if hasattr(CoHostOut, "from_model"):
        return CoHostOut.from_model(obj)  # helper yako
    return CoHostOut.model_validate(obj)  # v1

def _ensure_transition(old: str, new: str) -> None:
    if old == new:
        return
    if old not in {"pending", "accepted", "rejected", "canceled"}:
        raise HTTPException(status_code=409, detail="Unknown current status")
    if old != "pending" and new in {"accepted", "rejected"}:
        raise HTTPException(status_code=409, detail="Only pending invites can be accepted/rejected")
    if old in {"accepted", "rejected", "canceled"} and new == "canceled":
        raise HTTPException(status_code=409, detail="Invite already finalized")

def _can_read(invite: Any, user: User) -> bool:
    role = getattr(user, "role", "user")
    if role in {"admin", "owner"}:
        return True
    return invite.inviter_id == user.id or invite.invitee_id == user.id

# ===== CREATE (send invite) =====
@router.post(
    "/invite",
    response_model=CoHostOut,
    status_code=status.HTTP_201_CREATED,
    summary="Tuma mwaliko wa co-host (idempotent, anti-duplicate)"
)
def invite_cohost(
    data: CoHostCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    inviter_id = current_user.id

    _rate_ok(inviter_id)
    _idempotency_check(inviter_id, idempotency_key)

    if getattr(data, "invitee_id", None) == inviter_id:
        raise HTTPException(status_code=422, detail="You cannot invite yourself")

    # Zuia duplicate pending kwa stream_id + invitee_id
    q = db.query(CoHost).filter(
        CoHost.stream_id == data.stream_id,
        CoHost.inviter_id == inviter_id,
        CoHost.invitee_id == data.invitee_id,
    )
    if hasattr(CoHost, "status"):
        q = q.filter(CoHost.status == "pending")
    dup = q.first()
    if dup:
        raise HTTPException(status_code=409, detail="Pending invite already exists for this user on this stream")

    # Tengeneza rekodi
    kwargs = data.dict()
    kwargs["inviter_id"] = inviter_id
    if "status" in CoHost.__table__.columns.keys():  # type: ignore
        kwargs.setdefault("status", "pending")
    inv = CoHost(**kwargs)

    # timestamps
    now = _utcnow()
    if hasattr(inv, "created_at") and not getattr(inv, "created_at", None):
        inv.created_at = now
    if hasattr(inv, "updated_at"):
        inv.updated_at = now

    try:
        db.add(inv)
        db.commit()
        db.refresh(inv)
    except Exception as e:
        db.rollback()
        # ruhusu retry ya idempotency
        if idempotency_key:
            with suppress(Exception):
                _IDEMP.pop((inviter_id, idempotency_key), None)
        raise HTTPException(status_code=500, detail=f"Invite create failed: {e}")

    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = _etag(inv)

    # (hiari) audit
    with suppress(Exception):
        from backend.routes.audit_log import emit_audit  # type: ignore
        emit_audit(
            db,
            action="cohost.create",
            status="success",
            severity="info",
            actor_id=current_user.id,
            actor_email=getattr(current_user, "email", None),
            resource_type="cohost",
            resource_id=str(inv.id),
            meta={"stream_id": inv.stream_id, "invitee_id": inv.invitee_id},
        )

    return _serialize(inv)

# ===== LIST by stream (paged) =====
@router.get(
    "/list/{stream_id}",
    response_model=List[CoHostOut],
    summary="Orodha ya co-host invites kwa stream (pagination + sorting)"
)
def get_cohosts(
    stream_id: str,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status_eq: Optional[str] = Query(None),
    sort_by: str = Query("created_at"),
    order: str = Query("desc"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    q = db.query(CoHost).filter(CoHost.stream_id == stream_id)
    if status_eq and hasattr(CoHost, "status"):
        q = q.filter(CoHost.status == status_eq)
    q = q.order_by(_order_by(CoHost, sort_by, order))

    # ruhusu kusoma tu ukihusika au admin
    row = q.first()
    if row and not _can_read(row, current_user):
        raise HTTPException(status_code=403, detail="Not allowed to view this stream's invites")

    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    return [_serialize(r) for r in rows]

# ===== UPDATE status (accept/reject/cancel) =====
@router.put(
    "/update/{cohost_id}",
    response_model=CoHostOut,
    summary="Sasisha hali ya mwaliko (accept/reject/cancel) kulingana na mtumiaji"
)
def update_cohost(
    cohost_id: int,
    payload: CoHostStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    inv = db.query(CoHost).filter(CoHost.id == cohost_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invite not found")

    # ruhusa: invitee â†’ accept/reject; inviter â†’ cancel; admin/owner â†’ zote
    role = getattr(current_user, "role", "user")
    is_admin = role in {"admin", "owner"}

    if payload.status in {"accepted", "rejected"} and not is_admin:
        if inv.invitee_id != current_user.id:
            raise HTTPException(status_code=403, detail="Only invitee can accept/reject")
    if payload.status == "canceled" and not is_admin:
        if inv.inviter_id != current_user.id:
            raise HTTPException(status_code=403, detail="Only inviter can cancel")

    old = getattr(inv, "status", "pending")
    _ensure_transition(old, payload.status)

    inv.status = payload.status
    _apply_status_time(inv, payload.status)

    # weka reason kama fields zipo
    if payload.reason:
        if payload.status == "rejected" and hasattr(inv, "reject_reason"):
            inv.reject_reason = payload.reason
        if payload.status == "canceled" and hasattr(inv, "cancel_reason"):
            inv.cancel_reason = payload.reason

    db.commit()
    db.refresh(inv)
    return _serialize(inv)

# ===== DELETE =====
@router.delete("/{cohost_id}", summary="Futa mwaliko (inviter au admin)")
def delete_cohost(
    cohost_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    inv = db.query(CoHost).filter(CoHost.id == cohost_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invite not found")

    role = getattr(current_user, "role", "user")
    if role not in {"admin", "owner"} and inv.inviter_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only inviter can delete")

    db.delete(inv)
    db.commit()
    return {"message": "Invite deleted"}

