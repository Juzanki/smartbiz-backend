# backend/routes/moderation_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_, or_, func

from backend.db import get_db
from backend.models.moderation import ModerationAction
from backend.schemas.moderation_schema import ModerationActionCreate, ModerationActionOut

# Auth (enforce who can moderate)
from backend.dependencies import get_current_user
from backend.models.user import User

router = APIRouter(prefix="/moderation", tags=["Moderation"])

UTC_NOW = lambda: datetime.now(timezone.utc)

# ---- Config ----
ALLOWED_ROLES = {"admin", "owner", "moderator"}  # extend as needed

# If you want to strictly validate allowed actions, fill this set; otherwise leave None to accept any.
ALLOWED_ACTIONS: Optional[set[str]] = None  # e.g., {"ban", "mute", "timeout", "kick", "warn", "shadowban"}

# ---- Helpers ----
def _is_moderator(user: User) -> bool:
    return getattr(user, "role", None) in ALLOWED_ROLES

def _force_actor(payload: ModerationActionCreate, user_id: int) -> ModerationActionCreate:
    """Force the actor_id from session if the field exists in the schema/model."""
    updates = {}
    for name in ("actor_id", "moderator_id", "performed_by"):
        if hasattr(payload, name):
            updates[name] = user_id
    if not updates:
        return payload
    try:
        # Pydantic v2
        return payload.model_copy(update=updates)  # type: ignore[attr-defined]
    except AttributeError:
        # Pydantic v1
        return payload.copy(update=updates)  # type: ignore

def _set_defaults(values: dict) -> dict:
    """Apply common defaults if the model has those columns."""
    now = UTC_NOW()
    if "created_at" not in values and hasattr(ModerationAction, "created_at"):
        values["created_at"] = now
    # Active by default unless schema already sets it
    if hasattr(ModerationAction, "is_active") and "is_active" not in values:
        values["is_active"] = True
    # Optional automatic expiry: respect duration fields if provided in schema
    duration_secs = None
    for k in ("duration_seconds", "duration_secs", "duration"):
        if k in values and values[k]:
            try:
                duration_secs = int(values[k])
            except Exception:
                pass
            break
    if duration_secs and hasattr(ModerationAction, "expires_at") and "expires_at" not in values:
        values["expires_at"] = now + timedelta(seconds=max(1, duration_secs))
    return values

def _normalize_action(action: Optional[str]) -> Optional[str]:
    if not action:
        return None
    s = action.strip().lower()
    return s or None

def _can_target(actor: User, target_user_id: Optional[int]) -> bool:
    """Customize with role hierarchy if you have it."""
    # Example: prevent self-ban/mute unless actor is admin/owner.
    if target_user_id is not None and actor.id == target_user_id and actor.role not in {"admin", "owner"}:
        return False
    return True

# ---- Endpoints ----
@router.post(
    "/",
    response_model=ModerationActionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Apply a moderation action",
)
def apply_action(
    data: ModerationActionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Apply a moderation action to a room (e.g., ban/mute/timeout/kick/warn).
    - Requires role in {admin, owner, moderator}.
    - Forces `actor_id` from session (never trust client).
    - Optional `Idempotency-Key` to avoid duplicate submissions (if column/index exists).
    - If the same active action already exists for the same (room, target, action), returns that one with 200.
    """
    if not _is_moderator(current_user):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Optional strict action validation
    if ALLOWED_ACTIONS is not None:
        act = _normalize_action(getattr(data, "action", None))
        if act not in ALLOWED_ACTIONS:
            raise HTTPException(status_code=422, detail=f"Unsupported action '{act}'. Allowed: {sorted(ALLOWED_ACTIONS)}")

    # Optional targeting constraints
    target_id = getattr(data, "target_id", None)
    if not _can_target(current_user, target_id):
        raise HTTPException(status_code=403, detail="Cannot moderate this target")

    payload = _force_actor(data, current_user.id)
    values = _set_defaults(payload.dict())

    # Attach idempotency key if model supports it
    if idempotency_key and hasattr(ModerationAction, "idempotency_key"):
        values["idempotency_key"] = idempotency_key

    # Try create, fallback to existing on uniqueness conflicts
    try:
        action_obj = ModerationAction(**values)
        db.add(action_obj)
        db.commit()
        db.refresh(action_obj)
        return action_obj
    except IntegrityError:
        db.rollback()
        # Return existing active action for same (room, target, action) if present
        q = db.query(ModerationAction).filter(
            ModerationAction.room_id == getattr(payload, "room_id"),
            ModerationAction.action == getattr(payload, "action"),
        )
        if target_id is not None and hasattr(ModerationAction, "target_id"):
            q = q.filter(ModerationAction.target_id == target_id)
        if hasattr(ModerationAction, "is_active"):
            q = q.filter(ModerationAction.is_active.is_(True))
        existing = q.order_by(getattr(ModerationAction, "created_at", ModerationAction.id).desc()).first()
        if existing:
            # Return 200 OK for idempotent duplicate
            from fastapi import Response
            resp = Response(status_code=status.HTTP_200_OK)
            # FastAPI ignores returned Response if also returning a model; so just return object.
            return existing
        raise HTTPException(status_code=409, detail="Duplicate moderation action")
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to apply moderation action: {exc}")

@router.get(
    "/{room_id}",
    response_model=List[ModerationActionOut],
    summary="Get moderation history for a room (legacy list)",
)
def get_moderation_history(
    room_id: str,
    db: Session = Depends(get_db),
    # Optional filters & pagination (backward-compatible)
    action: Optional[str] = Query(None, description="Filter by action (e.g., ban, mute)"),
    target_id: Optional[int] = Query(None),
    actor_id: Optional[int] = Query(None),
    active_only: bool = Query(False, description="Only active/unexpired actions"),
    since: Optional[datetime] = Query(None, description="Return actions since this time"),
    until: Optional[datetime] = Query(None, description="Return actions up to this time"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
):
    """
    Returns moderation actions for a room with optional filters and pagination.
    Keeps the original response shape (list only).
    """
    q = db.query(ModerationAction).filter(ModerationAction.room_id == room_id)

    if action:
        q = q.filter(ModerationAction.action == _normalize_action(action))
    if target_id is not None and hasattr(ModerationAction, "target_id"):
        q = q.filter(ModerationAction.target_id == target_id)
    if actor_id is not None:
        # actor id field name may vary; try common ones
        if hasattr(ModerationAction, "actor_id"):
            q = q.filter(ModerationAction.actor_id == actor_id)
        elif hasattr(ModerationAction, "moderator_id"):
            q = q.filter(ModerationAction.moderator_id == actor_id)
        elif hasattr(ModerationAction, "performed_by"):
            q = q.filter(ModerationAction.performed_by == actor_id)

    now = UTC_NOW()
    if active_only:
        if hasattr(ModerationAction, "is_active"):
            q = q.filter(ModerationAction.is_active.is_(True))
        # If expiry exists, ensure not expired
        if hasattr(ModerationAction, "expires_at"):
            q = q.filter(or_(ModerationAction.expires_at.is_(None), ModerationAction.expires_at > now))
        # If revoked_at exists, exclude revoked
        if hasattr(ModerationAction, "revoked_at"):
            q = q.filter(ModerationAction.revoked_at.is_(None))

    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        q = q.filter(getattr(ModerationAction, "created_at", now) >= since)
    if until is not None:
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        q = q.filter(getattr(ModerationAction, "created_at", now) <= until)

    order_col = getattr(ModerationAction, "created_at", ModerationAction.id)
    q = q.order_by(order_col.asc() if sort == "asc" else order_col.desc())

    return q.offset(offset).limit(limit).all()

# Optional: paged shape with meta (great for mobile/infinite scroll)
from pydantic import BaseModel

class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int

class ModerationPage(BaseModel):
    items: List[ModerationActionOut]
    meta: PageMeta

@router.get("/{room_id}/page", response_model=ModerationPage, summary="Paged moderation history")
def get_moderation_history_paged(
    room_id: str,
    db: Session = Depends(get_db),
    action: Optional[str] = Query(None),
    target_id: Optional[int] = Query(None),
    actor_id: Optional[int] = Query(None),
    active_only: bool = Query(False),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
):
    base = get_moderation_history.__wrapped__(  # reuse logic; returns list but we need total too
        room_id=room_id, db=db, action=action, target_id=target_id, actor_id=actor_id,
        active_only=active_only, since=since, until=until, limit=10**9, offset=0, sort=sort  # type: ignore
    )
    total = len(base)
    items = base[offset: offset + limit]
    return ModerationPage(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))

@router.post("/{action_id}/revoke", response_model=ModerationActionOut, summary="Revoke/disable a moderation action")
def revoke_action(
    action_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Revoke (disable) an existing moderation action early.
    Requires moderator privileges.
    """
    if not _is_moderator(current_user):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    obj = db.query(ModerationAction).filter(ModerationAction.id == action_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Action not found")

    # If model supports these fields, update them appropriately.
    now = UTC_NOW()
    if hasattr(obj, "is_active"):
        obj.is_active = False
    if hasattr(obj, "revoked_at"):
        obj.revoked_at = now
    if hasattr(obj, "expires_at") and getattr(obj, "expires_at", None) and obj.expires_at > now:
        obj.expires_at = now

    db.commit()
    db.refresh(obj)
    return obj

@router.get(
    "/{room_id}/user/{target_id}/active",
    response_model=List[ModerationActionOut],
    summary="Active moderation actions for a user in a room",
)
def active_actions_for_user(
    room_id: str,
    target_id: int,
    db: Session = Depends(get_db),
):
    """
    Returns active/unexpired actions for a given user in a room (e.g., to enforce bans/mutes).
    """
    now = UTC_NOW()
    q = db.query(ModerationAction).filter(
        ModerationAction.room_id == room_id,
        getattr(ModerationAction, "target_id", None) == target_id if hasattr(ModerationAction, "target_id") else True,  # type: ignore
    )
    if hasattr(ModerationAction, "is_active"):
        q = q.filter(ModerationAction.is_active.is_(True))
    if hasattr(ModerationAction, "revoked_at"):
        q = q.filter(ModerationAction.revoked_at.is_(None))
    if hasattr(ModerationAction, "expires_at"):
        q = q.filter(or_(ModerationAction.expires_at.is_(None), ModerationAction.expires_at > now))

    order_col = getattr(ModerationAction, "created_at", ModerationAction.id)
    return q.order_by(order_col.desc()).all()

