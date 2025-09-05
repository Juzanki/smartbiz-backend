# backend/routes/top_contributors.py
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Top Contributors API (mobile-first, international-ready)

Endpoints
- POST /top-contributors/update                  -> increment/set a single contributor (concurrency-safe)
- POST /top-contributors/bulk                    -> bulk increment/set in one transaction
- GET  /top-contributors/stream/{stream_id}      -> leaderboard (limit + simple search)
- GET  /top-contributors/stream/{stream_id}/me   -> my current score (auth required, optional)

Notes
- Uses row-level locking to avoid lost updates under concurrency.
- Optional idempotency header, if your model has a column like `last_idempotency_key`.
- UTC ISO timestamps, compact/mobile responses.
"""
from datetime import datetime, timezone
from typing import List, Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from pydantic import BaseModel, Field, conint
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.db import get_db
from backend.models.top_contributor import TopContributor
from backend.schemas.top_contributor_schemas import TopContributorUpdate, TopContributorOut  # if you have it
# Optional: if you have auth
try:
    from backend.auth import get_current_user
    from backend.models.user import User
except Exception:  # pragma: no cover
    get_current_user = None  # type: ignore
    User = object  # type: ignore

router = APIRouter(prefix="/top-contributors", tags=["Top Contributors"])

# ---------- helpers ----------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if isinstance(dt, datetime) else None

# ---------- local fallbacks (if your project lacks Out schema) ----------
class _ContributorOut(BaseModel):
    id: int
    stream_id: int
    user_id: int
    total_value: int
    last_updated: Optional[str] = None

def _serialize(rec: TopContributor):
    # Use project schema if available
    try:
        return TopContributorOut.model_validate(rec)  # type: ignore
    except Exception:
        return _ContributorOut(
            id=getattr(rec, "id"),
            stream_id=getattr(rec, "stream_id"),
            user_id=getattr(rec, "user_id"),
            total_value=int(getattr(rec, "total_value") or 0),
            last_updated=_to_iso(getattr(rec, "last_updated", None)),
        )

# ---------- request/response models ----------
class UpdateMode(BaseModel):
    mode: Literal["increment", "set"] = Field("increment", description="How to apply `value`")

class BulkUpdateItem(TopContributorUpdate, UpdateMode):
    pass

class BulkUpdateBody(BaseModel):
    updates: List[BulkUpdateItem]

class PageMeta(BaseModel):
    count: int

class LeaderboardOut(BaseModel):
    meta: PageMeta
    items: List[_ContributorOut]  # falls back if your project schema isn't present

# ---------- routes ----------

@router.post(
    "/update",
    response_model=_ContributorOut,  # falls back cleanly; if you prefer your schema, change to TopContributorOut
    status_code=status.HTTP_201_CREATED,
    summary="Upsert a contributor (increment or set)",
)
def update_top_contributor(
    data: TopContributorUpdate,              # expects: stream_id, user_id, value
    db: Session = Depends(get_db),
    mode: Literal["increment", "set"] = Query("increment"),
    idempotency_key: Optional[str] = Header(
        None, convert_underscores=False, description="Optional idempotency key to prevent double-counting"
    ),
):
    """
    Concurrency-safe upsert:
    - Locks existing row (if any).
    - `mode=increment` (default) adds the value; `mode=set` overwrites the total.
    - If model has `last_idempotency_key` and it matches, no-op (idempotent).
    """
    # Try to lock existing row
    rec: Optional[TopContributor] = (
        db.query(TopContributor)
        .filter(TopContributor.stream_id == data.stream_id, TopContributor.user_id == data.user_id)
        .with_for_update()
        .one_or_none()
    )

    # Idempotency: if supported by your model
    if rec and idempotency_key and hasattr(rec, "last_idempotency_key"):
        if getattr(rec, "last_idempotency_key") == idempotency_key:
            # No change; return the current state
            return _serialize(rec)

    if rec:
        if mode == "set":
            rec.total_value = int(data.value)
        else:
            rec.total_value = int(rec.total_value or 0) + int(data.value)
        rec.last_updated = _utcnow()
        if idempotency_key and hasattr(rec, "last_idempotency_key"):
            rec.last_idempotency_key = idempotency_key  # type: ignore[attr-defined]
    else:
        rec = TopContributor(
            stream_id=data.stream_id,
            user_id=data.user_id,
            total_value=int(data.value) if mode == "set" else int(data.value),
            last_updated=_utcnow(),
        )
        if idempotency_key and hasattr(TopContributor, "last_idempotency_key"):
            setattr(rec, "last_idempotency_key", idempotency_key)  # type: ignore[attr-defined]
        db.add(rec)

    db.commit()
    db.refresh(rec)
    return _serialize(rec)


@router.post(
    "/bulk",
    response_model=LeaderboardOut,
    status_code=status.HTTP_201_CREATED,
    summary="Bulk upsert contributors in one transaction",
)
def bulk_update_top_contributors(
    body: BulkUpdateBody,
    db: Session = Depends(get_db),
    mode: Literal["increment", "set"] = Query("increment", description="Default mode for items missing `mode`"),
    idempotency_key: Optional[str] = Header(None, convert_underscores=False),
):
    """
    Applies multiple updates in a single transaction.
    - Each item can override `mode`; otherwise uses query's `mode`.
    - Best-effort idempotency if your model stores a single last idempotency key (coarse-grained).
    """
    updated: List[TopContributor] = []

    for item in body.updates:
        # Determine effective mode
        item_mode = getattr(item, "mode", None) or mode

        rec: Optional[TopContributor] = (
            db.query(TopContributor)
            .filter(TopContributor.stream_id == item.stream_id, TopContributor.user_id == item.user_id)
            .with_for_update()
            .one_or_none()
        )

        if rec and idempotency_key and hasattr(rec, "last_idempotency_key"):
            if getattr(rec, "last_idempotency_key") == idempotency_key:
                updated.append(rec)
                continue

        if rec:
            if item_mode == "set":
                rec.total_value = int(item.value)
            else:
                rec.total_value = int(rec.total_value or 0) + int(item.value)
            rec.last_updated = _utcnow()
            if idempotency_key and hasattr(rec, "last_idempotency_key"):
                rec.last_idempotency_key = idempotency_key  # type: ignore[attr-defined]
            updated.append(rec)
        else:
            rec = TopContributor(
                stream_id=item.stream_id,
                user_id=item.user_id,
                total_value=int(item.value) if item_mode == "set" else int(item.value),
                last_updated=_utcnow(),
            )
            if idempotency_key and hasattr(TopContributor, "last_idempotency_key"):
                setattr(rec, "last_idempotency_key", idempotency_key)  # type: ignore[attr-defined]
            db.add(rec)
            updated.append(rec)

    db.commit()

    # Order by total_value desc then id desc (simple leaderboard ordering)
    updated_sorted = sorted(updated, key=lambda r: (int(r.total_value or 0), int(getattr(r, "id", 0))), reverse=True)
    return LeaderboardOut(
        meta=PageMeta(count=len(updated_sorted)),
        items=[_serialize(r) for r in updated_sorted],  # type: ignore[list-item]
    )


@router.get(
    "/stream/{stream_id}",
    response_model=LeaderboardOut,
    summary="Get top contributors for a stream (leaderboard)",
)
def get_top_contributors(
    stream_id: int,
    db: Session = Depends(get_db),
    limit: conint(ge=1, le=100) = Query(10, description="Max number of rows"),
    q: Optional[str] = Query(None, max_length=64, description="Optional filter by user_id (string)"),
):
    """
    Lightweight leaderboard:
    - Orders by total_value DESC, then id DESC.
    - Optional search by exact user_id (string compare) for quick lookups.
    """
    query = db.query(TopContributor).filter(TopContributor.stream_id == stream_id)
    if q:
        # Simple filter: match user_id string
        query = query.filter(func.cast(TopContributor.user_id, db.bind.dialect.type_descriptor(func.cast.type)) == q)  # type: ignore

    rows = (
        query
        .order_by(TopContributor.total_value.desc(), TopContributor.id.desc())
        .limit(int(limit))
        .all()
    )

    return LeaderboardOut(
        meta=PageMeta(count=len(rows)),
        items=[_serialize(r) for r in rows],  # type: ignore[list-item]
    )


@router.get(
    "/stream/{stream_id}/me",
    response_model=_ContributorOut,
    summary="Get current user's contribution for a stream",
)
def get_my_contribution(
    stream_id: int,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user) if get_current_user else None,  # optional auth
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    rec = (
        db.query(TopContributor)
        .filter(TopContributor.stream_id == stream_id, TopContributor.user_id == current_user.id)
        .one_or_none()
    )
    if not rec:
        # Return a zeroed snapshot for UX (mobile-friendly)
        temp = TopContributor(
            id=0,  # not persisted
            stream_id=stream_id,
            user_id=current_user.id,
            total_value=0,
            last_updated=None,
        )
        return _serialize(temp)
    return _serialize(rec)

