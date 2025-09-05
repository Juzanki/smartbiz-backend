# backend/routes/leaderboard_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from zoneinfo import ZoneInfo

from backend.db import get_db
from backend.models.gift_movement import GiftMovement
# If you gate access, uncomment:
# from backend.auth import get_current_user
# from backend.models.user import User

router = APIRouter(prefix="/leaderboard", tags=["Leaderboards"])

# ---------- Schemas ----------
class LeaderboardEntry(BaseModel):
    sender_id: int
    total_value: Decimal = Field(..., description="Sum of GiftMovement.total_value in the period")
    gift_count: int = Field(..., description="Number of gifts by this sender in the period")
    rank: int = Field(..., description="1 = highest total")

    model_config = ConfigDict(from_attributes=True)

class LeaderboardPage(BaseModel):
    items: List[LeaderboardEntry]
    period_start: datetime
    period_end: datetime
    limit: int
    offset: int


# ---------- Helpers ----------
def _parse_tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:
        raise HTTPException(status_code=422, detail=f"Invalid timezone: {tz_name}")

def _day_bounds(tz: ZoneInfo, for_date: Optional[date] = None) -> tuple[datetime, datetime]:
    """Start/end of the given local day (default: today) converted to UTC."""
    local_today = for_date or datetime.now(tz).date()
    start_local = datetime(local_today.year, local_today.month, local_today.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

def _rolling_days_bounds(tz: ZoneInfo, days: int) -> tuple[datetime, datetime]:
    """Last `days` days up to end of current local day (exclusive), in UTC."""
    # End = start of tomorrow local
    _, end_utc = _day_bounds(tz)
    start_utc = (end_utc - timedelta(days=days))
    return start_utc, end_utc

def _rank_rows(rows: List[tuple]) -> List[LeaderboardEntry]:
    """
    rows: tuples with (sender_id, total_value, gift_count)
    Produces dense ranks (ties share rank).
    """
    ranked: List[LeaderboardEntry] = []
    last_val: Optional[Decimal] = None
    rank = 0
    idx = 0
    for sender_id, total_val, gift_count in rows:
        idx += 1
        # Normalize numeric types to Decimal for consistency
        total_dec = Decimal(str(total_val or 0))
        if last_val is None or total_dec != last_val:
            rank = idx
            last_val = total_dec
        ranked.append(LeaderboardEntry(sender_id=sender_id, total_value=total_dec, gift_count=int(gift_count or 0), rank=rank))
    return ranked

def _query_period(
    db: Session,
    stream_id: int,
    start_utc: datetime,
    end_utc: datetime,
    limit: int,
    offset: int,
):
    sum_expr = func.coalesce(func.sum(GiftMovement.total_value), 0)
    cnt_expr = func.count(GiftMovement.id)

    q = (
        db.query(
            GiftMovement.sender_id,
            sum_expr.label("total_value"),
            cnt_expr.label("gift_count"),
        )
        .filter(
            and_(
                GiftMovement.stream_id == stream_id,
                GiftMovement.sent_at >= start_utc,
                GiftMovement.sent_at < end_utc,
            )
        )
        .group_by(GiftMovement.sender_id)
        .order_by(sum_expr.desc(), GiftMovement.sender_id.asc())
        .offset(offset)
        .limit(limit)
    )
    return q.all()


# ---------- Endpoints ----------
@router.get("/daily/{stream_id}", response_model=LeaderboardPage, summary="Top senders today (local day)")
def daily_leaderboard(
    stream_id: int,
    db: Session = Depends(get_db),
    # current_user: User = Depends(get_current_user),  # enable if you need auth
    tz: str = Query("Africa/Dar_es_Salaam", description="IANA timezone used to define the local day"),
    date_override: Optional[date] = Query(None, description="Compute leaderboard for this local date instead of today"),
    limit: int = Query(10, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    tzinfo = _parse_tz(tz)
    start_utc, end_utc = _day_bounds(tzinfo, date_override)

    rows = _query_period(db, stream_id, start_utc, end_utc, limit, offset)
    items = _rank_rows(rows)
    return LeaderboardPage(
        items=items,
        period_start=start_utc,
        period_end=end_utc,
        limit=limit,
        offset=offset,
    )


@router.get("/weekly/{stream_id}", response_model=LeaderboardPage, summary="Top senders for the last 7 days (rolling)")
def weekly_leaderboard(
    stream_id: int,
    db: Session = Depends(get_db),
    tz: str = Query("Africa/Dar_es_Salaam"),
    days: int = Query(7, ge=1, le=31, description="Rolling window size in days"),
    limit: int = Query(10, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    tzinfo = _parse_tz(tz)
    start_utc, end_utc = _rolling_days_bounds(tzinfo, days)

    rows = _query_period(db, stream_id, start_utc, end_utc, limit, offset)
    items = _rank_rows(rows)
    return LeaderboardPage(
        items=items,
        period_start=start_utc,
        period_end=end_utc,
        limit=limit,
        offset=offset,
    )


@router.get("/range/{stream_id}", response_model=LeaderboardPage, summary="Top senders in a custom time range")
def range_leaderboard(
    stream_id: int,
    db: Session = Depends(get_db),
    start: datetime = Query(..., description="Start datetime (inclusive) in ISO8601; interpreted as UTC if no tz"),
    end: datetime = Query(..., description="End datetime (exclusive) in ISO8601; interpreted as UTC if no tz"),
    limit: int = Query(10, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    # Normalize naive datetimes to UTC
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    else:
        start = start.astimezone(timezone.utc)

    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    else:
        end = end.astimezone(timezone.utc)

    if end <= start:
        raise HTTPException(status_code=422, detail="`end` must be after `start`")

    rows = _query_period(db, stream_id, start, end, limit, offset)
    items = _rank_rows(rows)
    return LeaderboardPage(items=items, period_start=start, period_end=end, limit=limit, offset=offset)
