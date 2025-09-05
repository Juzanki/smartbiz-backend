from __future__ import annotations
# backend/routes/replay_counters.py
import logging
from contextlib import suppress
from datetime import datetime, timezone
from typing import Dict, Optional, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models.replay_analytics import ReplayAnalytics

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/replay-analytics", tags=["Replay Analytics Counters"])

# (hiari) ukitaka idempotency ya DB-level, tengeneza model hii:
# class ReplayAnalyticsKey(Base):  # unique (stream_id, key)
#     __tablename__ = "replay_analytics_keys"
#     id = Column(Integer, primary_key=True)
#     stream_id = Column(Integer, index=True, nullable=False)
#     key = Column(String(64), nullable=False)
#     created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
ReplayAnalyticsKey = None
with suppress(Exception):
    from backend.models.replay_analytics_key import ReplayAnalyticsKey  # type: ignore

# ---------- Schemas ----------
AllowedField = Literal["views", "likes", "comments", "shares", "downloads"]

class IncrementRequest(BaseModel):
    stream_id: int = Field(..., ge=1)
    # Njia 2: (i) field+amount AU (ii) fields dict
    field: Optional[AllowedField] = None
    amount: int = Field(1, ge=1)
    fields: Optional[Dict[AllowedField, int]] = None  # {"views":1,"likes":1}

    @validator("fields", always=True)
    def at_least_one_increment(cls, v, values):
        # Ikiwa fields haipo, tumia field+amount
        if v is None and not values.get("field"):
            raise ValueError("Provide either `field` or `fields`.")
        return v

# ---------- Helpers ----------
def _utc() -> datetime:
    return datetime.now(timezone.utc)

def _dialect(db: Session) -> str:
    try:
        return db.bind.dialect.name  # type: ignore[attr-defined]
    except Exception:
        return "unknown"

def _claim_idempotency(db: Session, stream_id: int, key: str) -> bool:
    """
    Jaribu kuhifadhi funguo ya idempotency. Ikiwa tayari ipo, rudisha False.
    Hii hufanya kazi tu ukiwa na modeli ReplayAnalyticsKey na UNIQUE(stream_id,key).
    """
    if not ReplayAnalyticsKey:
        # hakuna jedwali la funguo -> rudi True (ruhusu kuendelea tu)
        return True
    try:
        row = ReplayAnalyticsKey(stream_id=stream_id, key=key, created_at=_utc())
        db.add(row)
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    except Exception as e:
        db.rollback()
        logger.warning("Idempotency claim failed (non-fatal): %s", e)
        return True

def _normalize_increments(payload: IncrementRequest) -> Dict[str, int]:
    if payload.fields:
        # Safisha negative/zero & ruhusu tu columns halali
        clean = {}
        for k, v in payload.fields.items():
            if v and v > 0:
                clean[k] = int(v)
        if not clean:
            raise HTTPException(status_code=400, detail="No positive increments provided.")
        return clean
    # single field
    return {payload.field: max(1, int(payload.amount))}  # type: ignore[arg-type]

# ---------- GET (lightweight counters) ----------
@router.get(
    "/{stream_id}/counters",
    summary="Pata kaunta za sasa",
    response_model=dict
)
def get_counters(stream_id: int, db: Session = Depends(get_db)):
    row = db.query(ReplayAnalytics).filter(ReplayAnalytics.stream_id == stream_id).first()
    if not row:
        # zero defaults
        return {"stream_id": stream_id, "views": 0, "likes": 0, "comments": 0, "shares": 0, "downloads": 0}
    return {
        "stream_id": stream_id,
        "views": getattr(row, "views", 0) or 0,
        "likes": getattr(row, "likes", 0) or 0,
        "comments": getattr(row, "comments", 0) or 0,
        "shares": getattr(row, "shares", 0) or 0,
        "downloads": getattr(row, "downloads", 0) or 0,
    }

# ---------- POST increment (atomic + upsert + idempotency) ----------
@router.post(
    "/increment",
    summary="Ongeza kaunta (atomic + idempotent)",
    status_code=status.HTTP_200_OK,
    response_model=dict
)
def increment_field(
    data: IncrementRequest,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    # 1) Idempotency (DB-level ikiwa imewezeshwa)
    if idempotency_key:
        if not _claim_idempotency(db, data.stream_id, idempotency_key):
            # tayari ilishatumika -> rudisha hali ya sasa bila kuongeza
            return get_counters(data.stream_id, db)

    # 2) Tengeneza set ya increments
    incs = _normalize_increments(data)
    allowed = {"views", "likes", "comments", "shares", "downloads"}
    if any(k not in allowed for k in incs):
        raise HTTPException(status_code=400, detail="Unknown field in increments.")

    now = _utc()

    # 3) Jaribu atomic UPDATE kwanza
    set_clause = {}
    for k, v in incs.items():
        set_clause[getattr(ReplayAnalytics, k)] = getattr(ReplayAnalytics, k) + v
    if hasattr(ReplayAnalytics, "updated_at"):
        set_clause[ReplayAnalytics.updated_at] = now

    q = (
        update(ReplayAnalytics)
        .where(ReplayAnalytics.stream_id == data.stream_id)
        .values(**set_clause)
    )
    q.execution_options(synchronize_session=False)
    result = db.execute(q)
    rows = result.rowcount or 0

    if rows == 0:
        # 4) Hakuna rekodi -> fanya upsert ya generic:
        #    - jaribu kuunda record mpya
        new_row = ReplayAnalytics(stream_id=data.stream_id)
        # weka zero defaults kisha ongeza incs
        for f in allowed:
            setattr(new_row, f, 0)
        for k, v in incs.items():
            setattr(new_row, k, v)
        if hasattr(new_row, "created_at") and not getattr(new_row, "created_at", None):
            setattr(new_row, "created_at", now)
        if hasattr(new_row, "updated_at"):
            setattr(new_row, "updated_at", now)

        try:
            db.add(new_row)
            db.commit()
        except IntegrityError:
            # Race: mtu mwingine aliunda kati ya muda → rudia UPDATE
            db.rollback()
            result = db.execute(q)
            db.commit()
        except Exception:
            db.rollback()
            raise

    else:
        db.commit()

    # 5) Rudisha kaunta za sasa
    return get_counters(data.stream_id, db)
