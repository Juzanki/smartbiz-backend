# backend/routes/user_bots.py
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
User Bots API (mobile-first, international-ready)

Endpoints
- POST   /user-bots/                            -> create a bot (idempotent)
- GET    /user-bots/my                          -> list my bots (lightweight)
- GET    /user-bots/my/page                     -> list my bots with cursor pagination & search
- GET    /user-bots/{bot_id}                    -> get one of my bots
- PATCH  /user-bots/{bot_id}                    -> partial update (name, purpose, expiry, status*)
- POST   /user-bots/{bot_id}/renew              -> extend expiry by N days
- POST   /user-bots/{bot_id}/status             -> quick status change (active/paused/archived)*
- DELETE /user-bots/{bot_id}                    -> delete (soft if supported, else hard)
- GET    /user-bots/stats                       -> quick counts by status*

*Status endpoints are no-ops if your model lacks a `status` field.

Notes
- Concurrency-safe updates via row locks.
- Optional idempotency header to avoid duplicate creations.
- UTC ISO timestamps for robust clients.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Header,
    Query,
    status,
)
from pydantic import BaseModel, Field, conint, constr
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models.user_bot import UserBot
from backend.schemas.user_bot_schemas import UserBotCreate, UserBotOut
from backend.auth import get_current_user
from backend.models.user import User

router = APIRouter(prefix="/user-bots", tags=["User Bots"])

# ---------- mobile-first defaults ----------
DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100
MAX_NAME = 80
MAX_PURPOSE = 280

# ---------- helpers ----------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if isinstance(dt, datetime) else None

def _norm_name(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[:MAX_NAME]

def _norm_purpose(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    return s[:MAX_PURPOSE]

def _hasattr_safe(obj: Any, name: str) -> bool:
    try:
        return hasattr(obj, name)
    except Exception:
        return False

# ---------- tiny local schemas for expanded endpoints ----------
class PageMeta(BaseModel):
    next_cursor: Optional[int] = None
    count: int

class BotPageOut(BaseModel):
    meta: PageMeta
    items: List[UserBotOut]

class UserBotPatch(BaseModel):
    name: Optional[constr(min_length=1, max_length=MAX_NAME)] = None
    purpose: Optional[constr(max_length=MAX_PURPOSE)] = None
    bot_package_id: Optional[int] = Field(None, ge=1)
    expiry_date: Optional[datetime] = None  # absolute timestamp (UTC recommended)
    # If your model has 'status'
    status: Optional[Literal["active", "paused", "archived"]] = None
    metadata: Optional[Dict[str, Any]] = None  # if your model stores JSON

class RenewIn(BaseModel):
    days: conint(ge=1, le=365) = Field(..., description="Days to extend the expiry by")

class StatusIn(BaseModel):
    status: Literal["active", "paused", "archived"]

class OkResponse(BaseModel):
    ok: bool = True
    message: Optional[str] = None

# ---------- creation ----------
@router.post(
    "/",
    response_model=UserBotOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user bot (idempotent and mobile-friendly)",
)
def create_user_bot(
    bot: UserBotCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(
        None, convert_underscores=False, description="Optional key to prevent duplicate creations"
    ),
):
    """
    Creates a bot for the current user.
    - Trims name/purpose.
    - Ensures unique (user_id, name) to avoid duplicates.
    - If your UserBot model has `create_idempotency_key`, it will be stored.
    """
    name = _norm_name(bot.name)
    purpose = _norm_purpose(getattr(bot, "purpose", None))

    # Check conflict: user cannot create two bots with the same name
    exists = (
        db.query(UserBot)
        .filter(UserBot.user_id == current_user.id, UserBot.name.ilike(name))
        .first()
    )
    if exists:
        raise HTTPException(status_code=409, detail="A bot with this name already exists")

    new_bot = UserBot(
        name=name,
        purpose=purpose,
        bot_package_id=getattr(bot, "bot_package_id", None),
        user_id=current_user.id,
        expiry_date=getattr(bot, "expiry_date", None),  # allow client to set initial expiry
    )
    if idempotency_key and _hasattr_safe(UserBot, "create_idempotency_key"):
        setattr(new_bot, "create_idempotency_key", idempotency_key)  # type: ignore[attr-defined]

    db.add(new_bot)
    db.commit()
    db.refresh(new_bot)
    return UserBotOut.model_validate(new_bot)

# ---------- list (simple) ----------
@router.get(
    "/my",
    response_model=List[UserBotOut],
    summary="List my bots (lightweight)",
)
def get_my_bots(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(UserBot)
        .filter(UserBot.user_id == current_user.id)
        .order_by(UserBot.id.desc())
        .all()
    )
    return [UserBotOut.model_validate(r) for r in rows]

# ---------- list (paginated + search) ----------
@router.get(
    "/my/page",
    response_model=BotPageOut,
    summary="List my bots (cursor pagination + search)",
)
def page_my_bots(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    cursor_id: Optional[int] = Query(None, description="Paginate backward: id < cursor_id"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    q: Optional[str] = Query(None, max_length=80, description="Search by name or purpose"),
    status_eq: Optional[str] = Query(None, description="Filter by status (if supported)"),
):
    query = db.query(UserBot).filter(UserBot.user_id == current_user.id)
    if q:
        like = f"%{q.strip()}%"
        if _hasattr_safe(UserBot, "purpose"):
            query = query.filter((UserBot.name.ilike(like)) | (UserBot.purpose.ilike(like)))
        else:
            query = query.filter(UserBot.name.ilike(like))
    if status_eq and _hasattr_safe(UserBot, "status"):
        query = query.filter(UserBot.status == status_eq)
    if cursor_id:
        query = query.filter(UserBot.id < cursor_id)

    rows = query.order_by(UserBot.id.desc()).limit(limit).all()
    next_cursor = rows[-1].id if rows else None
    return BotPageOut(
        meta=PageMeta(next_cursor=next_cursor, count=len(rows)),
        items=[UserBotOut.model_validate(r) for r in rows],
    )

# ---------- get one ----------
@router.get(
    "/{bot_id}",
    response_model=UserBotOut,
    summary="Get a single bot I own",
)
def get_user_bot(
    bot_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    bot = (
        db.query(UserBot)
        .filter(UserBot.id == bot_id, UserBot.user_id == current_user.id)
        .one_or_none()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    return UserBotOut.model_validate(bot)

# ---------- partial update ----------
@router.patch(
    "/{bot_id}",
    response_model=UserBotOut,
    summary="Update fields of my bot (partial)",
)
def update_user_bot(
    bot_id: int,
    patch: UserBotPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    if_match: Optional[str] = Header(
        None, convert_underscores=False, description="Optional optimistic lock token if you add a 'version' field"
    ),
):
    bot = (
        db.query(UserBot)
        .filter(UserBot.id == bot_id, UserBot.user_id == current_user.id)
        .with_for_update()
        .one_or_none()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    data = patch.model_dump(exclude_unset=True)
    if "name" in data:
        data["name"] = _norm_name(data["name"])  # normalize
        # uniqueness per user
        clash = (
            db.query(UserBot)
            .filter(UserBot.user_id == current_user.id, UserBot.name.ilike(data["name"]), UserBot.id != bot_id)
            .first()
        )
        if clash:
            raise HTTPException(status_code=409, detail="Another bot with this name already exists")
    if "purpose" in data and data["purpose"] is not None:
        data["purpose"] = _norm_purpose(data["purpose"])

    for k, v in data.items():
        if _hasattr_safe(bot, k):
            setattr(bot, k, v)

    if _hasattr_safe(bot, "updated_at"):
        bot.updated_at = _utcnow()

    db.commit()
    db.refresh(bot)
    return UserBotOut.model_validate(bot)

# ---------- renew / extend expiry ----------
@router.post(
    "/{bot_id}/renew",
    response_model=UserBotOut,
    summary="Extend bot expiry by N days",
)
def renew_user_bot(
    bot_id: int,
    body: RenewIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    bot = (
        db.query(UserBot)
        .filter(UserBot.id == bot_id, UserBot.user_id == current_user.id)
        .with_for_update()
        .one_or_none()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    now = _utcnow()
    current_expiry: Optional[datetime] = getattr(bot, "expiry_date", None)
    base = current_expiry if (isinstance(current_expiry, datetime) and current_expiry > now) else now
    new_expiry = base + timedelta(days=int(body.days))
    if _hasattr_safe(bot, "expiry_date"):
        bot.expiry_date = new_expiry
    if _hasattr_safe(bot, "updated_at"):
        bot.updated_at = now

    db.commit()
    db.refresh(bot)
    return UserBotOut.model_validate(bot)

# ---------- quick status toggle (if supported) ----------
@router.post(
    "/{bot_id}/status",
    response_model=UserBotOut,
    summary="Change bot status quickly (active/paused/archived)",
)
def set_bot_status(
    bot_id: int,
    body: StatusIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    bot = (
        db.query(UserBot)
        .filter(UserBot.id == bot_id, UserBot.user_id == current_user.id)
        .with_for_update()
        .one_or_none()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if not _hasattr_safe(bot, "status"):
        raise HTTPException(status_code=501, detail="Status field is not supported by the model")

    bot.status = body.status  # type: ignore[attr-defined]
    if _hasattr_safe(bot, "updated_at"):
        bot.updated_at = _utcnow()
    db.commit()
    db.refresh(bot)
    return UserBotOut.model_validate(bot)

# ---------- delete (soft if supported, else hard) ----------
@router.delete(
    "/{bot_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete my bot (soft if supported, else hard)",
)
def delete_user_bot(
    bot_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    hard_delete: bool = Query(False, description="If true, attempt hard delete even if soft-delete exists"),
):
    bot = (
        db.query(UserBot)
        .filter(UserBot.id == bot_id, UserBot.user_id == current_user.id)
        .with_for_update()
        .one_or_none()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    # Soft delete path if model supports 'status' or 'deleted_at'
    if not hard_delete:
        try:
            if _hasattr_safe(bot, "status"):
                bot.status = "archived"  # or "deleted" depending on your enums
            if _hasattr_safe(bot, "deleted_at"):
                bot.deleted_at = _utcnow()
            if _hasattr_safe(bot, "updated_at"):
                bot.updated_at = _utcnow()
            db.commit()
            return
        except Exception:
            db.rollback()

    # Hard delete fallback
    db.delete(bot)
    db.commit()
    return

# ---------- quick stats ----------
@router.get(
    "/stats",
    response_model=Dict[str, int],
    summary="Quick counts of my bots (by status if available)",
)
def bots_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    base = db.query(UserBot).filter(UserBot.user_id == current_user.id)
    out: Dict[str, int] = {"total": base.count()}
    if _hasattr_safe(UserBot, "status"):
        for s in ["active", "paused", "archived"]:
            out[s] = base.filter(UserBot.status == s).count()
    return out

