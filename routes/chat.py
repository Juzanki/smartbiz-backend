from __future__ import annotations
# backend/routes/chat.py
import os
import time
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import suppress

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Response, Header
)
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User

# Schemas
from backend.schemas import ChatCreate, ChatOut

# CRUD (jaribu kutumia yako; kisha tuna fallbacks)
from backend.crud.chat_crud import create_message, get_messages_by_room

# (Hiari) jaribu model moja kwa moja kwa pagination ya haraka
with suppress(Exception):
    from backend.models.chat import ChatMessage  # kama upo

router = APIRouter(prefix="/chat", tags=["Chat"])

# ------------------------- Config & Limits ------------------------- #
MAX_MESSAGE_LEN = int(os.getenv("CHAT_MAX_MESSAGE_LEN", "5000"))
RATE_PER_MINUTE = int(os.getenv("CHAT_RATE_PER_MINUTE", "60"))  # per user
ROOM_RATE_PER_MINUTE = int(os.getenv("CHAT_ROOM_RATE_PER_MINUTE", "120"))  # per room per user
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
ALLOWED_ORDER = ("asc", "desc")

# Memory guards (badala ya Redis; unaweza kubadilisha baadaye)
_USER_RATE: Dict[int, List[float]] = {}
_ROOM_RATE: Dict[tuple[int, str], List[float]] = {}
_IDEMP: Dict[tuple[int, str], float] = {}
_IDEMP_TTL = 10 * 60  # sekunde

# ------------------------- Helpers ------------------------- #
def _rate_ok(user_id: int, room_id: Optional[str] = None) -> None:
    now = time.time()

    # Global per-user
    q = _USER_RATE.setdefault(user_id, [])
    while q and (now - q[0]) > 60.0:
        q.pop(0)
    if len(q) >= RATE_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Too many messages per minute")
    q.append(now)

    # Per-room per-user
    if room_id:
        key = (user_id, room_id)
        rq = _ROOM_RATE.setdefault(key, [])
        while rq and (now - rq[0]) > 60.0:
            rq.pop(0)
        if len(rq) >= ROOM_RATE_PER_MINUTE:
            raise HTTPException(status_code=429, detail="Room flood protection")
        rq.append(now)

def _idempotency_check(user_id: int, key: Optional[str]) -> None:
    if not key:
        return
    now = time.time()
    # safisha
    stale = [(k_uid, k) for (k_uid, k), ts in _IDEMP.items() if now - ts > _IDEMP_TTL]
    for s in stale:
        _IDEMP.pop(s, None)
    token = (user_id, key.strip())
    if token in _IDEMP:
        raise HTTPException(status_code=409, detail="Duplicate send (Idempotency-Key)")
    _IDEMP[token] = now

def _clamp_limit(limit: Optional[int]) -> int:
    if not limit:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))

def _p2_validate_text(txt: str) -> str:
    t = (txt or "").strip()
    if not t:
        raise HTTPException(status_code=422, detail="Message cannot be empty")
    if len(t) > MAX_MESSAGE_LEN:
        raise HTTPException(status_code=413, detail=f"Message too long (>{MAX_MESSAGE_LEN} chars)")
    return t

def _serialize_one(msg: Any) -> ChatOut:
    # Pydantic v1/v2 compatibility
    if hasattr(ChatOut, "model_validate"):
        return ChatOut.model_validate(msg, from_attributes=True)  # v2
    return ChatOut.model_validate(msg)  # v1

# ====================== ðŸ“¨ Tuma ujumbe mpya ====================== #
@router.post(
    "",
    response_model=ChatOut,
    status_code=status.HTTP_201_CREATED,
    summary="Tuma ujumbe mpya (idempotent, rate-limited)"
)
def send_message(
    chat: ChatCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    # ðŸ”’ usiruhusu spoof ya sender_id ndani ya payload
    # Lazima CRUD yako iitwe na sender_id wa sasa
    chat.room_id = chat.room_id.strip()
    chat.content = _p2_validate_text(chat.content)

    _rate_ok(current_user.id, chat.room_id)
    _idempotency_check(current_user.id, idempotency_key)

    # Ontoa "sender" upande wa server
    # hakikisha ChatCreate ina fields: room_id, content, (optional metadata)
    # na create_message(db, chat, sender_id=...) inakubali kwetu:
    try:
        msg = create_message(db, chat, sender_id=current_user.id)  # ðŸ’¡ ongeza parameter hii kwenye CRUD yako
    except TypeError:
        # fallback kama signature ya zamani haina sender_id
        # (bado itafanya kazi kama CRUD yako inaweka sender_id kutoka token)
        msg = create_message(db, chat)

    response.headers["Cache-Control"] = "no-store"
    return _serialize_one(msg)

# ====================== ðŸ“¥ Pata ujumbe wa chumba ====================== #
@router.get(
    "/{room_id}",
    response_model=List[ChatOut],
    summary="Pata ujumbe wa chumba (pagination + delta sync)"
)
def get_room_messages_route(
    room_id: str,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    order: str = Query("desc", description=f"Order: {', '.join(ALLOWED_ORDER)}"),
    # Cursor/delta:
    before_id: Optional[int] = Query(None, description="Leta messages < before_id (scroll back)"),
    after_id: Optional[int] = Query(None, description="Leta messages > after_id (delta sync)"),
    since: Optional[datetime] = Query(None, description="Leta messages tangu muda huu"),
    with_me: bool = Query(True, description="True: pamoja na ya mtumiaji; False: wa wengine tu"),
):
    """
    - Tumia **after_id** kwa delta sync (mobile poll).
    - Tumia **before_id** kwa infinite scroll kurudi nyuma.
    - `since` ni mbadala laini (kama huna ids).
    - `order` chagua asc/desc (default desc kwa mobile).
    """
    limit = _clamp_limit(limit)
    room_id = room_id.strip()

    # Jaribu njia ya haraka ikiwa una model ChatMessage
    if "ChatMessage" in globals():
        q = db.query(ChatMessage).filter(ChatMessage.room_id == room_id)

        # chujio la mmiliki / wengine
        if not with_me:
            q = q.filter(ChatMessage.sender_id != current_user.id)

        # cursors
        if before_id:
            q = q.filter(ChatMessage.id < int(before_id))
        if after_id:
            q = q.filter(ChatMessage.id > int(after_id))
        if since and hasattr(ChatMessage, "created_at"):
            q = q.filter(ChatMessage.created_at >= since)

        # order + limit
        q = q.order_by(ChatMessage.id.asc() if order == "asc" else ChatMessage.id.desc())
        rows = q.limit(limit).all()

        # next cursor (kwa scroll-back)
        next_cursor = None
        if rows and order == "desc":
            next_cursor = int(rows[-1].id)

        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Limit"] = str(limit)
        if next_cursor:
            response.headers["X-Next-Cursor"] = str(next_cursor)
        return [_serialize_one(r) for r in rows]

    # Fallback: tumia CRUD yako iliyopo
    rows = get_messages_by_room(db, room_id)

    # post-filter (si bora, lakini inafanya kazi)
    if not with_me:
        rows = [r for r in rows if getattr(r, "sender_id", None) != current_user.id]
    if since and hasattr(rows[0], "created_at") if rows else False:
        rows = [r for r in rows if r.created_at >= since]
    # rudisha kulingana na after/before
    def _cmp_id(x): return getattr(x, "id", 0) or 0
    rows.sort(key=_cmp_id, reverse=(order == "desc"))
    if after_id:
        rows = [r for r in rows if _cmp_id(r) > int(after_id)]
    if before_id:
        rows = [r for r in rows if _cmp_id(r) < int(before_id)]
    rows = rows[:limit]

    response.headers["Cache-Control"] = "no-store"
    return [_serialize_one(r) for r in rows]

# ====================== ðŸ”Ž Vidokezo vya uboreshaji wa CRUD ====================== #
# - create_message(db, chat, sender_id: int) â†’ weka sender_id kutoka dependency,
#   usiache client aitume (spoofing risk).
# - get_messages_by_room(db, room_id, *, limit, before_id, after_id, since, order)
#   ili pagination ifanyike kwenye DB (si Python).

