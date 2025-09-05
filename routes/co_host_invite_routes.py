from __future__ import annotations
# backend/routes/co_host_invites.py
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
from sqlalchemy import and_, or_, func

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User  # for type hints

# ---- Models ----
try:
    from backend.models.co_host_invite import CoHostInvite
except Exception:
    # Fail early with a clearer message if model is missing
    raise RuntimeError("Model 'CoHostInvite' haijapatikana. Tafadhali tengeneza backend/models/co_host_invite.py")

# ---- Schemas (tumia ulizo nazo; toa fallback kama hazipo) ----
try:
    from backend.schemas.co_host_invite import (
        CoHostInviteCreate, CoHostInviteOut, CoHostInviteStatusUpdate
    )
except Exception:
    from pydantic import BaseModel, Field

    class CoHostInviteCreate(BaseModel):
        inviter_id: int
        invitee_id: int
        event_id: Optional[int] = None
        message: Optional[str] = Field(None, max_length=500)

    class CoHostInviteStatusUpdate(BaseModel):
        status: Literal["accepted", "rejected", "canceled"]
        reason: Optional[str] = Field(None, max_length=300)

    class CoHostInviteOut(BaseModel):
        id: int
        inviter_id: int
        invitee_id: int
        event_id: Optional[int] = None
        message: Optional[str] = None
        status: str
        sent_at: Optional[datetime] = None
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None
        accepted_at: Optional[datetime] = None
        rejected_at: Optional[datetime] = None
        canceled_at: Optional[datetime] = None

router = APIRouter(prefix="/co-host-invites", tags=["CoHost Invites"])

# --------------------------- Config & helpers --------------------------- #
RATE_PER_MIN = int(os.getenv("COHOST_INVITE_RATE_PER_MIN", "20"))   # kwa inviter
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
ALLOWED_SORT = ("id", "sent_at", "created_at", "updated_at", "status")
ALLOWED_ORDER = ("asc", "desc")

_RATE: Dict[int, List[float]] = {}
_IDEMP: Dict[tuple[int, str], float] = {}
_IDEMP_TTL = 10 * 60  # sekunde

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
    # safisha keys zilizopitwa
    stale = [(k_uid, k) for (k_uid, k), ts in _IDEMP.items() if now - ts > _IDEMP_TTL]
    for s in stale:
        _IDEMP.pop(s, None)
    token = (uid, key.strip())
    if token in _IDEMP:
        raise HTTPException(status_code=409, detail="Duplicate request (Idempotency-Key)")
    _IDEMP[token] = now

def _clamp_limit(limit: Optional[int]) -> int:
    if not limit:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))

def _order_by_whitelist(model, sort_by: str, order: str):
    key = sort_by if sort_by in ALLOWED_SORT else "id"
    col = getattr(model, key, getattr(model, "id"))
    return col.asc() if order == "asc" else col.desc()

def _time_field():
    # tumia sent_at kama ipo, vinginevyo created_at
    return "sent_at" if hasattr(CoHostInvite, "sent_at") else "created_at"

def _etag(invite: Any) -> str:
    base = f"{getattr(invite, 'id', '')}-{getattr(invite, 'updated_at', '') or getattr(invite, 'status', '')}"
    return 'W/"' + hashlib.sha256(str(base).encode("utf-8")).hexdigest()[:16] + '"'

def _apply_timestamps_on_status(invite: Any, status_val: str) -> None:
    now = _utcnow()
    if status_val == "accepted" and hasattr(invite, "accepted_at"):
        invite.accepted_at = now
    if status_val == "rejected" and hasattr(invite, "rejected_at"):
        invite.rejected_at = now
    if status_val == "canceled" and hasattr(invite, "canceled_at"):
        invite.canceled_at = now
    if hasattr(invite, "updated_at"):
        invite.updated_at = now

# --------------------------- Guards & rules --------------------------- #
def _ensure_transition(old: str, new: str) -> None:
    if old == new:
        return
    if old not in {"pending", "accepted", "rejected", "canceled"}:
        raise HTTPException(status_code=409, detail="Unknown current status")
    if old != "pending" and new in {"accepted", "rejected"}:
        raise HTTPException(status_code=409, detail="Only pending invites can be accepted/rejected")
    if old in {"accepted", "rejected", "canceled"} and new == "canceled":
        raise HTTPException(status_code=409, detail="Invite already finalized")

def _ensure_actor_rights(action: str, invite: Any, user: User) -> None:
    """
    action: "accept"|"reject"|"cancel"|"delete"|"read"
    """
    role = getattr(user, "role", "user")
    is_admin = role in {"admin", "owner"}
    if is_admin:
        return
    if action in {"accept", "reject"} and invite.invitee_id != user.id:
        raise HTTPException(status_code=403, detail="Only the invitee can accept/reject")
    if action in {"cancel", "delete"} and invite.inviter_id != user.id:
        raise HTTPException(status_code=403, detail="Only the inviter can cancel/delete")
    # read: allow inviter or invitee
    if action == "read" and not (invite.inviter_id == user.id or invite.invitee_id == user.id):
        raise HTTPException(status_code=403, detail="Not allowed to view this invite")

# --------------------------- CREATE --------------------------- #
@router.post(
    "",
    response_model=CoHostInviteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an invite (idempotent, anti-duplicate)"
)
def send_invite(
    data: CoHostInviteCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    # inviter lazima awe current user (usikubali client kuchomeka inviter_id ya mtu mwingine)
    inviter_id = current_user.id
    if getattr(data, "inviter_id", inviter_id) != inviter_id:
        # override silently kwa usalama
        with suppress(Exception):
            data.inviter_id = inviter_id

    if data.inviter_id == data.invitee_id:
        raise HTTPException(status_code=422, detail="You cannot invite yourself")

    _rate_ok(inviter_id)
    _idempotency_check(inviter_id, idempotency_key)

    # Zuia duplicate pending kwa pair hii (event_id ikihusika)
    q = db.query(CoHostInvite).filter(
        CoHostInvite.inviter_id == data.inviter_id,
        CoHostInvite.invitee_id == data.invitee_id,
    )
    if hasattr(CoHostInvite, "event_id"):
        q = q.filter(CoHostInvite.event_id == getattr(data, "event_id", None))
    if hasattr(CoHostInvite, "status"):
        q = q.filter(CoHostInvite.status == "pending")
    existing = q.first()
    if existing:
        raise HTTPException(status_code=409, detail="A pending invite already exists for this pair")

    invite = CoHostInvite(**data.dict())

    # weka status & timestamps
    if hasattr(invite, "status") and not getattr(invite, "status", None):
        invite.status = "pending"
    now = _utcnow()
    if hasattr(invite, "sent_at") and not getattr(invite, "sent_at", None):
        invite.sent_at = now
    if hasattr(invite, "created_at") and not getattr(invite, "created_at", None):
        invite.created_at = now
    if hasattr(invite, "updated_at"):
        invite.updated_at = now

    try:
        db.add(invite)
        db.commit()
        db.refresh(invite)
    except Exception as e:
        db.rollback()
        # on failure, ruhusu retry ya idempotency key
        if idempotency_key:
            with suppress(Exception):
                _IDEMP.pop((inviter_id, idempotency_key), None)
        raise HTTPException(status_code=500, detail=f"Invite create failed: {e}")

    # Headers
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = _etag(invite)

    # (Hiari) toa audit log
    with suppress(Exception):
        from backend.routes.audit_log import emit_audit  # type: ignore
        emit_audit(
            db,
            action="cohost.invite.create",
            status="success",
            severity="info",
            actor_id=current_user.id,
            actor_email=getattr(current_user, "email", None),
            resource_type="invite",
            resource_id=str(invite.id),
            meta={"invitee_id": invite.invitee_id, "event_id": getattr(invite, "event_id", None)},
        )

    # Pydantic v1/v2
    if hasattr(CoHostInviteOut, "model_validate"):
        return CoHostInviteOut.model_validate(invite, from_attributes=True)  # type: ignore
    return CoHostInviteOut.model_validate(invite)  # type: ignore

# --------------------------- GET ONE --------------------------- #
@router.get("/{invite_id}", response_model=CoHostInviteOut, summary="Get a single invite")
def get_invite(
    invite_id: int = Path(..., ge=1),
    response: Response = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    invite = db.query(CoHostInvite).filter(CoHostInvite.id == invite_id).first()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    _ensure_actor_rights("read", invite, current_user)
    if response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["ETag"] = _etag(invite)
    return CoHostInviteOut.model_validate(invite, from_attributes=True) if hasattr(CoHostInviteOut, "model_validate") else CoHostInviteOut.model_validate(invite)  # type: ignore

# --------------------------- LIST (received/sent) --------------------------- #
@router.get(
    "/me/received",
    response_model=List[CoHostInviteOut],
    summary="Invites received by me (paged, sorted)"
)
def my_received_invites(
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status_eq: Optional[str] = Query(None, description="Filter by status"),
    event_id: Optional[int] = Query(None),
    sort_by: str = Query(_time_field()),
    order: str = Query("desc"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    q = db.query(CoHostInvite).filter(CoHostInvite.invitee_id == current_user.id)
    if status_eq and hasattr(CoHostInvite, "status"):
        q = q.filter(CoHostInvite.status == status_eq)
    if event_id is not None and hasattr(CoHostInvite, "event_id"):
        q = q.filter(CoHostInvite.event_id == event_id)

    q = q.order_by(_order_by_whitelist(CoHostInvite, sort_by, order))
    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    ser = [
        CoHostInviteOut.model_validate(r, from_attributes=True) if hasattr(CoHostInviteOut, "model_validate") else CoHostInviteOut.model_validate(r)  # type: ignore
        for r in rows
    ]
    return ser

@router.get(
    "/me/sent",
    response_model=List[CoHostInviteOut],
    summary="Invites I have sent (paged, sorted)"
)
def my_sent_invites(
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status_eq: Optional[str] = Query(None),
    event_id: Optional[int] = Query(None),
    sort_by: str = Query(_time_field()),
    order: str = Query("desc"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    q = db.query(CoHostInvite).filter(CoHostInvite.inviter_id == current_user.id)
    if status_eq and hasattr(CoHostInvite, "status"):
        q = q.filter(CoHostInvite.status == status_eq)
    if event_id is not None and hasattr(CoHostInvite, "event_id"):
        q = q.filter(CoHostInvite.event_id == event_id)

    q = q.order_by(_order_by_whitelist(CoHostInvite, sort_by, order))
    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    ser = [
        CoHostInviteOut.model_validate(r, from_attributes=True) if hasattr(CoHostInviteOut, "model_validate") else CoHostInviteOut.model_validate(r)  # type: ignore
        for r in rows
    ]
    return ser

# --------------------------- UPDATE STATUS --------------------------- #
@router.put("/{invite_id}/status", response_model=CoHostInviteOut, summary="Accept/Reject/Cancel an invite")
def update_invite_status(
    invite_id: int,
    payload: CoHostInviteStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    invite = db.query(CoHostInvite).filter(CoHostInvite.id == invite_id).first()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    # rights
    if payload.status in {"accepted", "rejected"}:
        _ensure_actor_rights(payload.status, invite, current_user)
    elif payload.status == "canceled":
        _ensure_actor_rights("cancel", invite, current_user)
    else:
        raise HTTPException(status_code=422, detail="Unsupported status")

    old = getattr(invite, "status", "pending")
    _ensure_transition(old, payload.status)

    invite.status = payload.status
    _apply_timestamps_on_status(invite, payload.status)

    # weka reason kama kuna field (e.g., reject_reason/cancel_reason) â€“ best effort
    if payload.reason:
        if payload.status == "rejected" and hasattr(invite, "reject_reason"):
            invite.reject_reason = payload.reason
        if payload.status == "canceled" and hasattr(invite, "cancel_reason"):
            invite.cancel_reason = payload.reason

    db.commit()
    db.refresh(invite)

    # (Hiari) audit
    with suppress(Exception):
        from backend.routes.audit_log import emit_audit  # type: ignore
        emit_audit(
            db,
            action=f"cohost.invite.{payload.status}",
            status="success",
            severity="info",
            actor_id=current_user.id,
            actor_email=getattr(current_user, "email", None),
            resource_type="invite",
            resource_id=str(invite.id),
        )

    return CoHostInviteOut.model_validate(invite, from_attributes=True) if hasattr(CoHostInviteOut, "model_validate") else CoHostInviteOut.model_validate(invite)  # type: ignore

# --------------------------- DELETE --------------------------- #
@router.delete("/{invite_id}", summary="Delete an invite (owner or admin)")
def delete_invite(
    invite_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    invite = db.query(CoHostInvite).filter(CoHostInvite.id == invite_id).first()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    _ensure_actor_rights("delete", invite, current_user)
    db.delete(invite)
    db.commit()

    with suppress(Exception):
        from backend.routes.audit_log import emit_audit  # type: ignore
        emit_audit(
            db,
            action="cohost.invite.delete",
            status="success",
            severity="low",
            actor_id=current_user.id,
            actor_email=getattr(current_user, "email", None),
            resource_type="invite",
            resource_id=str(invite_id),
        )

    return {"message": "Invite deleted"}

