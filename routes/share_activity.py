from __future__ import annotations
# backend/routes/share_activity.py
import hashlib
from collections import defaultdict
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Response, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.db import get_db

# ===================== Pydantic v2/v1 compatibility =====================
try:
    from pydantic import BaseModel, Field, ConfigDict
    _P2 = True
except Exception:  # Pydantic v1 fallback
    from pydantic import BaseModel, Field  # type: ignore
    ConfigDict = dict  # type: ignore
    _P2 = False


# ============================== Schemas ==============================
class ShareActivityBase(BaseModel):
    """Schema ya msingi—ina config MOJA tu (hakuna migongano)."""
    # core
    actor_id: int = Field(..., description="User initiating the share")
    target_type: str = Field(..., description="e.g. product|post|video")
    target_id: str = Field(..., max_length=120)
    channel: str = Field(..., description="whatsapp|sms|email|link|facebook|x")
    message: Optional[str] = Field(None, max_length=2000)

    # client/device
    platform: Optional[str] = Field(None, description="android|ios|web")
    app_version: Optional[str] = None
    device_model: Optional[str] = None
    locale: Optional[str] = None
    country: Optional[str] = None

    # extras/metrics
    metadata: Optional[Dict[str, Any]] = None
    tags: Optional[List[str]] = None

    # server managed
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # **CONFIG**: tumia style MOJA tu kulingana na version
    if _P2:
        model_config = ConfigDict(from_attributes=True, extra="ignore")
    else:
        class Config:  # type: ignore
            orm_mode = True
            extra = "ignore"


class ShareActivityCreate(ShareActivityBase):
    """Payload ya kuunda Share Activity."""
    pass


class ShareActivityOut(ShareActivityBase):
    """Response schema kwa Share Activity."""
    id: int
    # NOTE: hakuna model_config/Config hapa—tunategemea config ya Base pekee.


# ============================== Model ==============================
# ORM model (lazima uwepo). Tukikosa, toa error ya kueleweka.
try:
    from backend.models.share_activity import ShareActivity as SA
except Exception as e:
    raise RuntimeError("⚠️ Missing model: backend.models.share_activity.ShareActivity") from e


# ============================== Router ==============================
router = APIRouter(prefix="/share-activity", tags=["Share Activity"])


# ============================== Utils ==============================
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: Optional[datetime]) -> datetime:
    if not dt:
        return _utc_now()
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _serialize(obj: Any) -> ShareActivityOut:
    """V2: model_validate(from_attributes=True); V1: from_orm"""
    if hasattr(ShareActivityOut, "model_validate"):
        # Pydantic v2
        return ShareActivityOut.model_validate(obj, from_attributes=True)
    # Pydantic v1
    return ShareActivityOut.from_orm(obj)  # type: ignore[attr-defined]


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


# ============================= POST / ==============================
@router.post(
    "",
    response_model=ShareActivityOut,
    status_code=status.HTTP_201_CREATED,
    summary="Rekodi tukio la kushare (idempotent + anti-duplicate)"
)
def log_share(
    data: ShareActivityCreate,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    dedupe_window_sec: int = Query(
        10, ge=0, le=300,
        description="Sekunde za kuzuia duplicate mfululizo"
    ),
):
    # Idempotency kwa kutumia column ya DB kama ipo
    if idempotency_key and hasattr(SA, "idempotency_key"):
        hit = db.query(SA).filter(SA.idempotency_key == idempotency_key).first()
        if hit:
            return _serialize(hit)

    now = _utc_now()
    created_at = _ensure_utc(getattr(data, "created_at", None))

    # Anti-duplicate window (user/room/platform/target ndani ya muda mfupi)
    if dedupe_window_sec > 0 and hasattr(SA, "created_at"):
        q = db.query(SA)
        # Tunatumia tuple ya vitambulisho vilivyo common; adjust ikihitajika
        if hasattr(SA, "actor_id"):
            q = q.filter(SA.actor_id == data.actor_id)
        if hasattr(SA, "target_type"):
            q = q.filter(SA.target_type == data.target_type)
        if hasattr(SA, "target_id"):
            q = q.filter(SA.target_id == data.target_id)
        if getattr(data, "platform", None) and hasattr(SA, "platform"):
            q = q.filter(SA.platform == data.platform)
        if getattr(data, "channel", None) and hasattr(SA, "channel"):
            q = q.filter(SA.channel == data.channel)

        q = q.filter(SA.created_at >= now - timedelta(seconds=dedupe_window_sec))
        dup = q.first()
        if dup:
            return _serialize(dup)

    # Tunga row kutoka kwa payload
    row = SA(**data.dict())

    # timestamps & idempotency
    if hasattr(row, "created_at") and not getattr(row, "created_at", None):
        row.created_at = created_at
    if hasattr(row, "updated_at"):
        row.updated_at = now
    if idempotency_key and hasattr(row, "idempotency_key"):
        row.idempotency_key = idempotency_key

    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize(row)


# ===================== GET /by-room/{room_id} =====================
@router.get(
    "/by-room/{room_id}",
    response_model=List[ShareActivityOut],
    summary="Orodha ya shares za chumba (filters + pagination + ETag)"
)
def get_room_shares(
    room_id: str,
    response: Response,
    db: Session = Depends(get_db),
    platform: Optional[str] = Query(None, description="Filter platform"),
    since: Optional[datetime] = Query(None, description="UTC ISO start"),
    until: Optional[datetime] = Query(None, description="UTC ISO end"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    q = db.query(SA).filter(SA.target_type == "room", SA.target_id == room_id) \
        if hasattr(SA, "target_type") and hasattr(SA, "target_id") \
        else db.query(SA).filter(SA.room_id == room_id)  # fallback ikiwa una column room_id

    if platform and hasattr(SA, "platform"):
        q = q.filter(SA.platform == platform)
    if since and hasattr(SA, "created_at"):
        q = q.filter(SA.created_at >= _ensure_utc(since))
    if until and hasattr(SA, "created_at"):
        q = q.filter(SA.created_at <= _ensure_utc(until))

    order_col = getattr(SA, "created_at", getattr(SA, "id"))
    q = q.order_by(order_col.asc() if order == "asc" else order_col.desc())

    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    tag = _etag(rows, extra=f"{room_id}|{platform}|{since}|{until}|{limit}|{offset}|{order}")
    if if_none_match and if_none_match == tag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "public, max-age=15"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    return [_serialize(r) for r in rows]


# ===================== GET /by-user/{user_id} =====================
@router.get(
    "/by-user/{user_id}",
    response_model=List[ShareActivityOut],
    summary="Orodha ya shares za mtumiaji (filters + pagination + ETag)"
)
def get_user_shares(
    user_id: int,
    response: Response,
    db: Session = Depends(get_db),
    platform: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    q = db.query(SA).filter(SA.actor_id == user_id) if hasattr(SA, "actor_id") else db.query(SA).filter(SA.user_id == user_id)

    if platform and hasattr(SA, "platform"):
        q = q.filter(SA.platform == platform)
    if since and hasattr(SA, "created_at"):
        q = q.filter(SA.created_at >= _ensure_utc(since))
    if until and hasattr(SA, "created_at"):
        q = q.filter(SA.created_at <= _ensure_utc(until))

    order_col = getattr(SA, "created_at", getattr(SA, "id"))
    q = q.order_by(order_col.asc() if order == "asc" else order_col.desc())

    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    tag = _etag(rows, extra=f"{user_id}|{platform}|{since}|{until}|{limit}|{offset}|{order}")
    if if_none_match and if_none_match == tag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "public, max-age=15"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    return [_serialize(r) for r in rows]


# ===================== GET /stats/room/{room_id} =====================
class RoomStatsOut(BaseModel):
    room_id: str
    since: datetime
    until: datetime
    total: int
    by_platform: Dict[str, int]
    daily: List[Dict[str, Any]]


@router.get(
    "/stats/room/{room_id}",
    response_model=RoomStatsOut,
    summary="Takwimu za sharing kwa chumba: jumla, kwa platform, na kwa siku"
)
def room_stats(
    room_id: str,
    db: Session = Depends(get_db),
    days: int = Query(7, ge=1, le=90),
):
    until = _utc_now()
    since = until - timedelta(days=days)

    # by platform (SQL GROUP BY)
    by_platform: Dict[str, int] = {}
    if hasattr(SA, "platform"):
        rows = (
            db.query(SA.platform, func.count(SA.id))
            .filter(
                (SA.target_type == "room") if hasattr(SA, "target_type") else SA.room_id == room_id,
                (SA.target_id == room_id) if hasattr(SA, "target_id") else True,
                SA.created_at >= since,
                SA.created_at <= until,
            )
            .group_by(SA.platform)
            .all()
        )
        by_platform = {str(p or "unknown"): int(c) for p, c in rows}
    else:
        total = (
            db.query(SA)
            .filter(
                (SA.target_type == "room") if hasattr(SA, "target_type") else SA.room_id == room_id,
                (SA.target_id == room_id) if hasattr(SA, "target_id") else True,
                SA.created_at >= since,
                SA.created_at <= until,
            )
            .count()
        )
        by_platform = {"all": total}

    # daily series (DB native for Postgres; fallback Python)
    try:
        dialect = str(db.get_bind().dialect.name)
        if dialect.startswith("postgre"):
            day_col = func.date_trunc("day", SA.created_at)
            drows = (
                db.query(day_col.label("day"), func.count(SA.id))
                .filter(
                    (SA.target_type == "room") if hasattr(SA, "target_type") else SA.room_id == room_id,
                    (SA.target_id == room_id) if hasattr(SA, "target_id") else True,
                    SA.created_at >= since,
                    SA.created_at <= until,
                )
                .group_by("day")
                .order_by("day")
                .all()
            )
            daily = [{"day": d.isoformat(), "count": int(c)} for d, c in drows]
        else:
            raise RuntimeError("fallback")
    except Exception:
        dmap: Dict[str, int] = defaultdict(int)
        rows = (
            db.query(SA)
            .filter(
                (SA.target_type == "room") if hasattr(SA, "target_type") else SA.room_id == room_id,
                (SA.target_id == room_id) if hasattr(SA, "target_id") else True,
                SA.created_at >= since,
                SA.created_at <= until,
            )
            .all()
        )
        for r in rows:
            dkey = (_ensure_utc(getattr(r, "created_at", until))).date().isoformat()
            dmap[dkey] += 1
        daily = [{"day": k, "count": v} for k, v in sorted(dmap.items())]

    total = sum(by_platform.values()) if by_platform else 0
    return RoomStatsOut(room_id=room_id, since=since, until=until, total=total, by_platform=by_platform, daily=daily)
