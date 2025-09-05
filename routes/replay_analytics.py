from __future__ import annotations
# backend/routes/replay_analytics.py
import hashlib
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from fastapi import (
    APIRouter, Depends, HTTPException, status, Response, Request, Header, Query, Body
)
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, text

from backend.db import get_db
from backend.models.replay_analytics import ReplayAnalytics
from backend.schemas.replay_analytics_schemas import (
    ReplayAnalyticsCreate, ReplayAnalyticsOut
)

router = APIRouter(prefix="/replay-analytics", tags=["Replay Analytics"])

# ---------------- Helpers ----------------

def _utc() -> datetime:
    return datetime.now(timezone.utc)

def _client_meta(req: Request) -> Dict[str, Any]:
    return {
        "ip": (req.client.host if req.client else None),
        "ua": req.headers.get("user-agent"),
        "ref": req.headers.get("referer") or req.headers.get("origin"),
    }

def _etag_of(rows: List[Any]) -> str:
    if not rows:
        return 'W/"empty"'
    last = max(
        getattr(r, "updated_at", None)
        or getattr(r, "timestamp", None)
        or getattr(r, "created_at", None)
        or datetime.min
        for r in rows
    )
    seed = f"{len(rows)}|{last.isoformat()}"
    return 'W/"' + hashlib.sha256(seed.encode()).hexdigest()[:16] + '"'

def _set_attrs(obj: Any, data: Dict[str, Any]) -> None:
    for k, v in data.items():
        if hasattr(obj, k):
            setattr(obj, k, v)

def _dialect(db: Session) -> str:
    with suppress(Exception):
        return db.bind.dialect.name  # type: ignore[attr-defined]
    return "unknown"

# ---------------- Create (single, idempotent + dedup) ----------------

@router.post(
    "",
    response_model=ReplayAnalyticsOut,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest tukio moja (idempotent + dedup window)"
)
def save_analytics(
    data: ReplayAnalyticsCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    dedup_seconds: int = Query(15, ge=0, le=600, description="Zuia duplicates ndani ya dirisha hili"),
):
    """
    - Tumia `Idempotency-Key` kuzuia retries (mobile network).
    - `dedup_seconds` huziba spams za tukio lilelile (stream_id+event+platform+position?).
    """
    # 1) Idempotency kwa column ikiwa ipo
    if idempotency_key and hasattr(ReplayAnalytics, "idempotency_key"):
        existing = (
            db.query(ReplayAnalytics)
            .filter(ReplayAnalytics.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            response.headers["Cache-Control"] = "no-store"
            return existing

    payload = data.dict()
    # ongeza meta ya mteja endapo model ina `meta`
    client_meta = _client_meta(request)
    if hasattr(ReplayAnalytics, "meta"):
        payload["meta"] = {**payload.get("meta", {})} if payload.get("meta") else {}
        payload["meta"].update(client_meta)

    # 2) Dedup window (best-effort)
    if dedup_seconds > 0:
        ts = payload.get("timestamp") or _utc()
        if not isinstance(ts, datetime):
            ts = _utc()
        win_start = ts - timedelta(seconds=dedup_seconds)
        filters = [
            ReplayAnalytics.stream_id == payload.get("stream_id"),
            ReplayAnalytics.event == payload.get("event"),
            ReplayAnalytics.timestamp >= win_start,
            ReplayAnalytics.timestamp <= ts,
        ]
        if "platform" in payload and hasattr(ReplayAnalytics, "platform"):
            filters.append(ReplayAnalytics.platform == payload["platform"])
        if "position" in payload and hasattr(ReplayAnalytics, "position"):
            # round position to nearest second if available
            try:
                pos = int(round(float(payload["position"])))
                filters.append(ReplayAnalytics.position == pos)
            except Exception:
                pass
        dup = db.query(ReplayAnalytics).filter(and_(*filters)).first()
        if dup:
            response.headers["Cache-Control"] = "no-store"
            return dup

    # 3) Tunga row
    row = ReplayAnalytics()
    _set_attrs(row, payload)
    if idempotency_key and hasattr(row, "idempotency_key"):
        row.idempotency_key = idempotency_key
    if hasattr(row, "created_at") and not getattr(row, "created_at", None):
        row.created_at = _utc()
    if hasattr(row, "updated_at"):
        row.updated_at = _utc()

    db.add(row)
    db.commit()
    db.refresh(row)
    response.headers["Cache-Control"] = "no-store"
    return row

# ---------------- Batch ingest (hupunguza roundtrips) ----------------

@router.post(
    "/batch",
    response_model=Dict[str, Any],
    summary="Ingest batch ya matukio (≤ 500)"
)
def save_analytics_batch(
    items: List[ReplayAnalyticsCreate] = Body(..., embed=True),
    request: Request = None,
    db: Session = Depends(get_db),
    limit: int = Query(500, ge=1, le=1000),
):
    if not items:
        return {"inserted": 0}

    items = items[:limit]
    client_meta = _client_meta(request) if request else {}
    inserted = 0
    now = _utc()

    for d in items:
        row = ReplayAnalytics()
        payload = d.dict()
        if hasattr(ReplayAnalytics, "meta"):
            payload["meta"] = {**payload.get("meta", {})} if payload.get("meta") else {}
            payload["meta"].update(client_meta)
        _set_attrs(row, payload)
        if hasattr(row, "created_at") and not getattr(row, "created_at", None):
            row.created_at = now
        if hasattr(row, "updated_at"):
            row.updated_at = now
        db.add(row)
        inserted += 1

    db.commit()
    return {"inserted": inserted}

# ---------------- Fetch raw (range + ETag + pagination) ----------------

@router.get(
    "/{stream_id}",
    response_model=List[ReplayAnalyticsOut],
    summary="Pata matukio (raw) kwa stream_id"
)
def get_analytics(
    stream_id: int,
    response: Response,
    db: Session = Depends(get_db),
    since: Optional[datetime] = Query(None, description="ISO start time"),
    until: Optional[datetime] = Query(None, description="ISO end time"),
    event: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    q = db.query(ReplayAnalytics).filter(ReplayAnalytics.stream_id == stream_id)
    if since:
        q = q.filter(ReplayAnalytics.timestamp >= since)
    if until:
        q = q.filter(ReplayAnalytics.timestamp <= until)
    if event and hasattr(ReplayAnalytics, "event"):
        q = q.filter(ReplayAnalytics.event == event)
    if platform and hasattr(ReplayAnalytics, "platform"):
        q = q.filter(ReplayAnalytics.platform == platform)

    order_col = getattr(ReplayAnalytics, "timestamp", None) or getattr(ReplayAnalytics, "id")
    q = q.order_by(order_col.asc() if order == "asc" else order_col.desc())

    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    etag = _etag_of(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=304)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=10"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    return rows

# ---------------- Downsampled series (raw/1m/5m/1h) ----------------

@router.get(
    "/{stream_id}/series",
    summary="Time-series aggregated (downsample: raw|1m|5m|1h)",
    response_model=List[Dict[str, Any]]
)
def analytics_series(
    stream_id: int,
    db: Session = Depends(get_db),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    event: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    bucket: str = Query("1m", pattern="^(raw|1m|5m|1h)$"),
    limit_points: int = Query(2000, ge=10, le=10000),
):
    """
    Rudisha [{bucket_start, count, event, platform}] kulingana na bucket.
    - Postgres: date_trunc hutumika.
    - SQLite: strftime fallback.
    - raw: hurudisha matukio kama yalivyo (limit_points).
    """
    if bucket == "raw":
        q = db.query(ReplayAnalytics).filter(ReplayAnalytics.stream_id == stream_id)
        if since:
            q = q.filter(ReplayAnalytics.timestamp >= since)
        if until:
            q = q.filter(ReplayAnalytics.timestamp <= until)
        if event and hasattr(ReplayAnalytics, "event"):
            q = q.filter(ReplayAnalytics.event == event)
        if platform and hasattr(ReplayAnalytics, "platform"):
            q = q.filter(ReplayAnalytics.platform == platform)
        q = q.order_by(ReplayAnalytics.timestamp.asc()).limit(limit_points)
        rows = q.all()
        return [
            {"bucket_start": getattr(r, "timestamp"), "count": 1,
             "event": getattr(r, "event", None), "platform": getattr(r, "platform", None)}
            for r in rows
        ]

    dial = _dialect(db)
    # Amua resolution
    if bucket == "1m":
        trunc = ("minute", "%Y-%m-%d %H:%M:00")
    elif bucket == "5m":
        trunc = ("minute", "%Y-%m-%d %H:%M:00")
    else:
        trunc = ("hour", "%Y-%m-%d %H:00:00")

    ts = ReplayAnalytics.timestamp
    if dial == "postgresql":
        texpr = func.date_trunc(trunc[0], ts).label("bucket_start")
    elif dial in ("sqlite", "sqlite3"):
        fmt = trunc[1]
        texpr = func.datetime(func.strftime(fmt, ts)).label("bucket_start")
    else:
        # generic fallback
        texpr = func.date(ts).label("bucket_start")

    q = db.query(
        texpr,
        func.count().label("count"),
        ReplayAnalytics.event if hasattr(ReplayAnalytics, "event") else text("NULL"),
        ReplayAnalytics.platform if hasattr(ReplayAnalytics, "platform") else text("NULL"),
    ).filter(ReplayAnalytics.stream_id == stream_id)

    if since:
        q = q.filter(ts >= since)
    if until:
        q = q.filter(ts <= until)
    if event and hasattr(ReplayAnalytics, "event"):
        q = q.filter(ReplayAnalytics.event == event)
    if platform and hasattr(ReplayAnalytics, "platform"):
        q = q.filter(ReplayAnalytics.platform == platform)

    group_cols = [texpr]
    if hasattr(ReplayAnalytics, "event"):
        group_cols.append(ReplayAnalytics.event)
    if hasattr(ReplayAnalytics, "platform"):
        group_cols.append(ReplayAnalytics.platform)

    q = q.group_by(*group_cols).order_by(texpr.asc()).limit(limit_points)
    rows = q.all()

    out = []
    for bucket_start, count, ev, pf in rows:
        out.append({
            "bucket_start": bucket_start,
            "count": int(count),
            "event": ev,
            "platform": pf
        })
    return out

# ---------------- Summary (by event/platform) ----------------

@router.get(
    "/{stream_id}/summary",
    summary="Muhtasari: jumla, by event, by platform",
    response_model=Dict[str, Any]
)
def analytics_summary(
    stream_id: int,
    db: Session = Depends(get_db),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
):
    ts = ReplayAnalytics.timestamp
    q = db.query(ReplayAnalytics).filter(ReplayAnalytics.stream_id == stream_id)
    if since:
        q = q.filter(ts >= since)
    if until:
        q = q.filter(ts <= until)

    total = q.count()

    by_event = {}
    if hasattr(ReplayAnalytics, "event"):
        rows = (
            q.with_entities(ReplayAnalytics.event, func.count())
             .group_by(ReplayAnalytics.event)
             .all()
        )
        by_event = {k: int(v) for k, v in rows}

    by_platform = {}
    if hasattr(ReplayAnalytics, "platform"):
        rows = (
            q.with_entities(ReplayAnalytics.platform, func.count())
             .group_by(ReplayAnalytics.platform)
             .all()
        )
        by_platform = {k: int(v) for k, v in rows}

    return {"total": int(total), "by_event": by_event, "by_platform": by_platform}

# ---------------- HEAD: ETag ya haraka ----------------

@router.head(
    "/{stream_id}",
    include_in_schema=False
)
def head_analytics(stream_id: int, db: Session = Depends(get_db)):
    q = db.query(ReplayAnalytics).filter(ReplayAnalytics.stream_id == stream_id)
    rows = q.order_by(ReplayAnalytics.timestamp.desc()).limit(1).all()
    etag = _etag_of(rows)
    return Response(status_code=204, headers={"ETag": etag, "Cache-Control": "public, max-age=10"})
