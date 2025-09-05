# backend/routes/host_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from pydantic import BaseModel, Field

from backend.dependencies import get_db, get_current_user
from backend.schemas.host_schemas import (
    CoHostInviteCreate,
    CoHostInviteOut,
    CoHostInviteUpdate,
)
from backend.crud import host_crud

# Optional: import model for direct checks when CRUD lacks helpers
try:
    from backend.models.co_host_invite import CoHostInvite  # type: ignore
except Exception:  # pragma: no cover
    CoHostInvite = None  # type: ignore

# Optional: WebSocket broadcast (ignore if not used)
try:
    from backend.utils.websocket_manager import WebSocketManager

    ws_manager: Optional[WebSocketManager] = WebSocketManager()
except Exception:  # pragma: no cover
    ws_manager = None  # type: ignore


router = APIRouter(tags=["Hosts & Co-Hosts"])

# ---- Config / helpers --------------------------------------------------------

ALLOWED_APPROVERS = {"admin", "owner"}  # add "moderator" if your app supports it
FINAL_STATUSES = {"accepted", "rejected", "canceled", "expired"}
VALID_STATUSES = {"pending", *FINAL_STATUSES}


def _is_admin(user) -> bool:
    return getattr(user, "role", None) in ALLOWED_APPROVERS


def _normalize_status(value: str) -> str:
    return (value or "").strip().lower()


def _force_sender(payload: CoHostInviteCreate, sender_id: int) -> CoHostInviteCreate:
    """
    Force sender_id from the authenticated user; never trust client input.
    """
    try:
        return payload.model_copy(update={"sender_id": sender_id})  # pydantic v2
    except AttributeError:  # pydantic v1
        return payload.copy(update={"sender_id": sender_id})


def _fetch_invite(db: Session, invite_id: int):
    """
    Try to fetch invite using CRUD; fall back to direct DB if model is available.
    """
    try:
        return host_crud.get_invite(db, invite_id)  # type: ignore[attr-defined]
    except Exception:
        pass
    if CoHostInvite:
        # .get works on SQLAlchemy 1.4/2.x session
        return db.get(CoHostInvite, invite_id)  # type: ignore
    return None


def _ensure_transition_allowed(current_status: str, target_status: str):
    cur = _normalize_status(current_status)
    tgt = _normalize_status(target_status)
    if tgt not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")
    if cur in FINAL_STATUSES:
        raise HTTPException(status_code=409, detail="Invite already finalized")
    if cur == "pending" and tgt in {"accepted", "rejected", "canceled"}:
        return
    # Anything else is not allowed (e.g., pending -> pending)
    raise HTTPException(status_code=409, detail="Invalid status transition")


# ---- Pagination DTOs ---------------------------------------------------------

class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int


class InvitePage(BaseModel):
    items: List[CoHostInviteOut]
    meta: PageMeta


# ---- Endpoints ---------------------------------------------------------------

@router.post(
    "/invite",
    response_model=CoHostInviteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Send a co-host invite",
)
def send_invite(
    invite: CoHostInviteCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Create (send) a co-host invite.
    - Forces `sender_id` from the authenticated user.
    - (Optional) Idempotency-Key to prevent duplicate submissions.
    - Prevent sending an invite to yourself.
    """
    payload = _force_sender(invite, user.id)

    # Basic validation: no self-invite
    if getattr(payload, "recipient_id", None) == user.id:
        raise HTTPException(status_code=400, detail="You cannot invite yourself")

    # If your app requires host rights for the stream, enforce here (pseudo):
    # if not stream_crud.user_is_host(db, stream_id=payload.stream_id, user_id=user.id):
    #     raise HTTPException(status_code=403, detail="Only stream host can invite co-hosts")

    try:
        # If CRUD supports idempotency_key, pass it through
        try:
            created = host_crud.create_invite(
                db, sender_id=user.id, invite=payload, idempotency_key=idempotency_key  # type: ignore
            )
        except TypeError:
            created = host_crud.create_invite(db, sender_id=user.id, invite=payload)
    except IntegrityError as ie:
        db.rollback()
        # e.g., UNIQUE(stream_id, sender_id, recipient_id) WHERE status='pending'
        raise HTTPException(status_code=409, detail="Duplicate invite") from ie
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create invite") from exc

    # Optional broadcast
    if ws_manager:
        try:
            ws_manager.broadcast(str(getattr(created, "stream_id", "")), {  # type: ignore
                "type": "cohost_invite_created",
                "invite_id": getattr(created, "id", None),
                "sender_id": getattr(created, "sender_id", None),
                "recipient_id": getattr(created, "recipient_id", None),
                "status": getattr(created, "status", "pending"),
            })
        except Exception:
            pass

    return created


@router.put(
    "/invite/{invite_id}",
    response_model=CoHostInviteOut,
    summary="Respond to a co-host invite (accept/reject/cancel)",
)
def respond_invite(
    invite_id: int,
    update: CoHostInviteUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Update invite status.
    Rules:
      - Only recipient can `accept` or `reject` a pending invite.
      - Only sender can `cancel` a pending invite.
      - Admin/Owner can perform any transition.
    """
    target_status = _normalize_status(update.status)

    # Fetch current invite
    invite_obj = _fetch_invite(db, invite_id)
    if not invite_obj:
        raise HTTPException(status_code=404, detail="Invite not found")

    current_status = getattr(invite_obj, "status", "pending")
    _ensure_transition_allowed(current_status, target_status)

    sender_id = getattr(invite_obj, "sender_id", None)
    recipient_id = getattr(invite_obj, "recipient_id", None)

    # Ownership/role checks (unless admin)
    if not _is_admin(user):
        if target_status in {"accepted", "rejected"} and user.id != recipient_id:
            raise HTTPException(status_code=403, detail="Only recipient can accept/reject this invite")
        if target_status == "canceled" and user.id != sender_id:
            raise HTTPException(status_code=403, detail="Only sender can cancel this invite")

    # Perform update via CRUD
    try:
        try:
            updated = host_crud.update_invite_status(  # type: ignore[attr-defined]
                db, invite_id, target_status, idempotency_key=idempotency_key
            )
        except TypeError:
            updated = host_crud.update_invite_status(db, invite_id, target_status)
    except IntegrityError as ie:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate update (idempotency)") from ie
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update invite") from exc

    # Optional broadcast
    if ws_manager:
        try:
            ws_manager.broadcast(str(getattr(updated, "stream_id", "")), {  # type: ignore
                "type": "cohost_invite_updated",
                "invite_id": getattr(updated, "id", None),
                "status": getattr(updated, "status", None),
            })
        except Exception:
            pass

    return updated


@router.get(
    "/invites/{stream_id}",
    response_model=List[CoHostInviteOut],
    summary="List invites for a stream (legacy list)",
)
def list_invites(
    stream_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    status_filter: Optional[str] = Query(None, description="pending|accepted|rejected|canceled|expired"),
    mine: Optional[str] = Query(None, description="Filter by relation: 'incoming' (to me) | 'outgoing' (from me)"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("created_at"),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
):
    """
    List all co-host invites for a given stream.
    Supports basic filters & pagination but returns a plain list (for backward compatibility).
    """
    # Prefer a richer CRUD if you have it
    try:
        return host_crud.get_stream_invites(
            db,
            stream_id,
            status_filter=status_filter,  # type: ignore
            mine=mine,  # type: ignore
            user_id=user.id,  # for 'incoming'/'outgoing' disambiguation
            limit=limit,
            offset=offset,
            order=order,
            sort=sort,
        )
    except TypeError:
        # Fall back to original signature and slice in Python
        items = host_crud.get_stream_invites(db, stream_id)
        if status_filter:
            sf = _normalize_status(status_filter)
            items = [it for it in items if _normalize_status(getattr(it, "status", "")) == sf]
        if mine in {"incoming", "outgoing"}:
            if mine == "incoming":
                items = [it for it in items if getattr(it, "recipient_id", None) == user.id]
            else:
                items = [it for it in items if getattr(it, "sender_id", None) == user.id]
        # naive sort by created_at if present
        reverse = (sort == "desc")
        try:
            items.sort(key=lambda x: getattr(x, order), reverse=reverse)  # type: ignore
        except Exception:
            pass
        return items[offset : offset + limit]


# Optional: paginated variant (items + meta) for mobile infinite scroll
@router.get(
    "/invites/{stream_id}/page",
    response_model=InvitePage,
    summary="List invites for a stream (paged)",
)
def list_invites_page(
    stream_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    status_filter: Optional[str] = Query(None),
    mine: Optional[str] = Query(None),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    try:
        total, items = host_crud.get_stream_invites_paged(  # type: ignore[attr-defined]
            db, stream_id, status_filter=status_filter, mine=mine, user_id=user.id, limit=limit, offset=offset
        )
        return InvitePage(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))
    except Exception:
        # Fallback: use non-paged CRUD + slice
        items = host_crud.get_stream_invites(db, stream_id)
        if status_filter:
            sf = _normalize_status(status_filter)
            items = [it for it in items if _normalize_status(getattr(it, "status", "")) == sf]
        if mine in {"incoming", "outgoing"}:
            if mine == "incoming":
                items = [it for it in items if getattr(it, "recipient_id", None) == user.id]
            else:
                items = [it for it in items if getattr(it, "sender_id", None) == user.id]
        total = len(items)
        return InvitePage(items=items[offset : offset + limit], meta=PageMeta(total=total, limit=limit, offset=offset))
