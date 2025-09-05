# -*- coding: utf-8 -*-
from __future__ import annotations
# backend/routes/superchats.py
"""
Superchats API (mobile-first, international-ready)

Backwards compatible with your existing CRUD:
- If a helper exists in superchat_crud (or wallet_crud), it's used.
- Otherwise, safe fallbacks keep endpoints working.

Endpoints
- POST   /superchats/                                -> send a superchat (with optional idempotency & cooldown)
- GET    /superchats/{stream_id}                     -> list superchats (lightweight, returns list only)
- GET    /superchats/{stream_id}/page                -> cursor pagination + filters (mobile-friendly)
- POST   /superchats/{superchat_id}/pin              -> pin (host/mod only if supported)
- POST   /superchats/{superchat_id}/unpin            -> unpin
- POST   /superchats/{superchat_id}/hide             -> hide (moderation)
- DELETE /superchats/{stream_id}/{superchat_id}      -> hard delete (if allowed)
"""
import re
from datetime import datetime, timezone
from typing import List, Optional, Literal, Dict, Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Header,
    status,
)
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.schemas.superchat import SuperchatCreate, SuperchatOut
from backend.crud import superchat_crud

# ---- Pydantic v2 preferred; v1 fallback (only for response helpers below) ----
_V2 = True
try:
    from pydantic import BaseModel
except Exception:  # pragma: no cover
    _V2 = False
    from pydantic import BaseModel  # type: ignore

# Optional wallet integration (if present in your project)
try:
    from backend.crud import wallet_crud  # type: ignore
except Exception:  # pragma: no cover
    wallet_crud = None  # type: ignore

router = APIRouter(prefix="/superchats", tags=["Superchats"])

# -------- Mobile-first constraints & defaults --------
MAX_MESSAGE_CHARS = 160       # keep chips readable on small screens
DEFAULT_PAGE_SIZE = 30        # sensible mobile page
MAX_PAGE_SIZE = 100
DEFAULT_COOLDOWN_SEC = 2      # soft cooldown hook
DEFAULT_PER_MIN_CAP = 12      # soft per-minute cap hook

# -------- Helpers --------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _normalize_message(text: str) -> str:
    # Collapse whitespace and trim, enforce max length
    msg = re.sub(r"\s+", " ", (text or "")).strip()
    if len(msg) > MAX_MESSAGE_CHARS:
        msg = msg[:MAX_MESSAGE_CHARS].rstrip()
    return msg

def _ensure_can_send(db: Session, user_id: int, stream_id: str) -> None:
    """
    Ask CRUD if sending is allowed (cooldown / per-minute cap / bans).
    If the helper doesn't exist, do nothing (allow).
    """
    guard = getattr(superchat_crud, "user_can_send", None)
    if callable(guard):
        allowed, reason = guard(
            db,
            user_id=user_id,
            stream_id=stream_id,
            cooldown_sec=DEFAULT_COOLDOWN_SEC,
            per_min_cap=DEFAULT_PER_MIN_CAP,
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=reason or "Rate limit",
            )

def _extract_stream_id(sc: SuperchatCreate) -> str:
    # Accept either stream_id or room_id on payload to be schema-agnostic.
    sid = getattr(sc, "stream_id", None) or getattr(sc, "room_id", None)
    if not sid:
        raise HTTPException(status_code=400, detail="Missing stream_id on payload")
    return str(sid)

# -------- Routes --------
@router.post(
    "/",
    response_model=SuperchatOut,
    status_code=status.HTTP_201_CREATED,
    summary="Send a superchat",
)
def send_superchat(
    sc: SuperchatCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(
        None,
        convert_underscores=False,
        description="Optional key to prevent duplicates if CRUD supports it",
    ),
    debit_wallet: bool = Query(
        False,
        description="If true and wallet_crud is available, debit user's wallet by sc.amount",
    ),
    publish_realtime: bool = Query(
        True,
        description="If CRUD exposes publish_event, broadcast this superchat",
    ),
) -> SuperchatOut:
    """
    Creates a superchat record after optional wallet debit and rate checks.
    """
    # Basic sanitation on message text (if your schema has it)
    if hasattr(sc, "message"):
        cleaned = _normalize_message(getattr(sc, "message"))
        if not cleaned:
            raise HTTPException(status_code=400, detail="Message cannot be empty")
        setattr(sc, "message", cleaned)

    # Enforce send permissions (cooldown/caps/bans if supported)
    stream_id = _extract_stream_id(sc)
    _ensure_can_send(db, current_user.id, stream_id)

    # Optional wallet debit (atomic if your CRUD handles transactions)
    if debit_wallet and wallet_crud and hasattr(sc, "amount"):
        amt = getattr(sc, "amount") or 0
        if amt > 0:
            debit_kwargs: Dict[str, Any] = {}
            if idempotency_key:
                debit_kwargs["idempotency_key"] = idempotency_key
            wallet_crud.debit(  # type: ignore[attr-defined]
                db,
                user_id=current_user.id,
                amount=amt,
                reason="superchat",
                metadata={"stream_id": stream_id},
                **debit_kwargs,
            )

    # Create the superchat (CRUD may support idempotency_key & user_id)
    create_fn = getattr(superchat_crud, "create_superchat", None)
    if not callable(create_fn):
        raise HTTPException(status_code=500, detail="superchat_crud.create_superchat not implemented")

    try:
        # Prefer signature with user_id & idempotency_key if implemented
        superchat = create_fn(db, sc, user_id=current_user.id, idempotency_key=idempotency_key)  # type: ignore[misc]
    except TypeError:
        # Fallback to original signature
        superchat = create_fn(db, sc)

    # Optional realtime publish/broadcast
    if publish_realtime:
        publish_fn = getattr(superchat_crud, "publish_event", None)
        if callable(publish_fn):
            try:
                publish_fn(db, "superchat.created", superchat)
            except Exception:
                # Non-fatal: don't block API if broker is down
                pass

    return superchat


@router.get(
    "/{stream_id}",
    response_model=List[SuperchatOut],
    summary="List superchats (lightweight)",
)
def list_superchats(
    stream_id: str,
    db: Session = Depends(get_db),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    since_id: Optional[int] = Query(None, description="Return items with id > since_id (for polling)"),
    viewer: Literal["public", "host", "moderator"] = Query("public", description="Visibility filter"),
) -> List[SuperchatOut]:
    """
    Lightweight listing for simple feeds. Use /page for cursor pagination.
    """
    rich_list = getattr(superchat_crud, "list_superchats", None)
    if callable(rich_list):
        return rich_list(
            db,
            stream_id=str(stream_id),
            limit=limit,
            since_id=since_id,
            viewer=viewer,
        )

    # Fallback: original get by stream
    if hasattr(superchat_crud, "get_superchats_by_stream"):
        items = superchat_crud.get_superchats_by_stream(db, str(stream_id))
        if since_id is not None:
            items = [it for it in items if getattr(it, "id", 0) > since_id]
        return items[:limit]

    raise HTTPException(status_code=500, detail="No list function available in superchat_crud")


# ----- Cursor pagination with filters & meta -----
class CursorPageMeta(BaseModel):
    next_cursor: Optional[int] = None
    count: int

class CursorPageOut(BaseModel):
    meta: CursorPageMeta
    items: List[SuperchatOut]

@router.get(
    "/{stream_id}/page",
    response_model=CursorPageOut,
    summary="List superchats with cursor pagination and filters",
)
def page_superchats(
    stream_id: str,
    db: Session = Depends(get_db),
    cursor_id: Optional[int] = Query(None, description="Start from id < cursor_id (backward pagination)"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    min_amount: Optional[int] = Query(None, ge=0, description="Filter by minimum amount (tier)"),
    user_id: Optional[int] = Query(None, ge=1, description="Filter by sender"),
    viewer: Literal["public", "host", "moderator"] = Query("public", description="Visibility filter"),
) -> CursorPageOut:
    """
    Backwards cursor pagination (mobile-friendly 'load more' pattern).
    """
    page_fn = getattr(superchat_crud, "page_superchats", None)
    if callable(page_fn):
        result = page_fn(
            db,
            stream_id=str(stream_id),
            cursor_id=cursor_id,
            limit=limit,
            min_amount=min_amount,
            user_id=user_id,
            viewer=viewer,
        )
        # Expecting dict-like: {"items": [...], "next_cursor": 123}
        items = result.get("items", [])
        next_cursor = result.get("next_cursor")
        return CursorPageOut(meta=CursorPageMeta(next_cursor=next_cursor, count=len(items)), items=items)

    # Fallback constructed on top of a basic list
    list_fn = getattr(superchat_crud, "list_superchats", None)
    if not callable(list_fn):
        raise HTTPException(status_code=500, detail="Pagination requires list_superchats or page_superchats in CRUD")

    items = list_fn(
        db,
        stream_id=str(stream_id),
        limit=limit,
        before_id=cursor_id,  # many implementations use before_id for backward paging
        min_amount=min_amount,
        user_id=user_id,
        viewer=viewer,
    )
    next_cursor = items[-1].id if items else None
    return CursorPageOut(meta=CursorPageMeta(next_cursor=next_cursor, count=len(items)), items=list(items))


# ----- Moderation: pin/unpin, hide, delete -----
class ModerationResponse(BaseModel):
    ok: bool
    action: str
    superchat_id: int

@router.post(
    "/{superchat_id}/pin",
    response_model=ModerationResponse,
    summary="Pin a superchat (host/mod)",
)
def pin_superchat(
    superchat_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fn = getattr(superchat_crud, "pin_superchat", None)
    if not callable(fn):
        raise HTTPException(status_code=501, detail="Pin operation not supported")
    ok = fn(db, superchat_id, actor_id=current_user.id)
    return ModerationResponse(ok=bool(ok), action="pin", superchat_id=superchat_id)

@router.post(
    "/{superchat_id}/unpin",
    response_model=ModerationResponse,
    summary="Unpin a superchat (host/mod)",
)
def unpin_superchat(
    superchat_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fn = getattr(superchat_crud, "unpin_superchat", None)
    if not callable(fn):
        raise HTTPException(status_code=501, detail="Unpin operation not supported")
    ok = fn(db, superchat_id, actor_id=current_user.id)
    return ModerationResponse(ok=bool(ok), action="unpin", superchat_id=superchat_id)

@router.post(
    "/{superchat_id}/hide",
    response_model=ModerationResponse,
    summary="Hide a superchat (moderation)",
)
def hide_superchat(
    superchat_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fn = getattr(superchat_crud, "hide_superchat", None)
    if not callable(fn):
        raise HTTPException(status_code=501, detail="Hide operation not supported")
    ok = fn(db, superchat_id, actor_id=current_user.id)
    return ModerationResponse(ok=bool(ok), action="hide", superchat_id=superchat_id)

@router.delete(
    "/{stream_id}/{superchat_id}",
    response_model=ModerationResponse,
    summary="Hard delete a superchat",
)
def delete_superchat(
    stream_id: str,
    superchat_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fn = getattr(superchat_crud, "delete_superchat", None)
    if not callable(fn):
        raise HTTPException(status_code=501, detail="Delete operation not supported")
    ok = fn(db, stream_id=str(stream_id), superchat_id=superchat_id, actor_id=current_user.id)
    return ModerationResponse(ok=bool(ok), action="delete", superchat_id=superchat_id)

