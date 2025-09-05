from __future__ import annotations
# backend/routes/gift_timeline.py
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter, Depends, HTTPException, Query, Header, Response
)
from sqlalchemy.orm import Session
from sqlalchemy import and_

from backend.db import get_db

# Model: tunatarajia haya mashamba yapo: id, stream_id, gift_name, sent_at (datetime), position (float)
GiftFly = None
try:
    from backend.models.gift_fly import GiftFly  # type: ignore
except Exception:
    pass

router = APIRouter(prefix="/replay", tags=["Replay Timeline"])

# ---------- helpers ----------
def _utc() -> datetime:
    return datetime.now(timezone.utc)

def _etag(rows: List[Any], extra: str = "") -> str:
    if not rows:
        seed = f"0|{extra}"
    else:
        last = max(getattr(r, "sent_at", None) or getattr(r, "id", 0) for r in rows)
        seed = f"{len(rows)}|{last}|{extra}"
    return 'W/"' + hashlib.sha256(seed.encode()).hexdigest()[:16] + '"'

def _compact_row(r: Any) -> Dict[str, Any]:
    # compact kwa mobile: fungua jina fupi
    return {
        "g": r.gift_name,
        "t": (r.sent_at.isoformat() if getattr(r, "sent_at", None) else None),
        "p": getattr(r, "position", None),
        "id": getattr(r, "id", None),
    }

def _full_row(r: Any) -> Dict[str, Any]:
    return {
        "id": getattr(r, "id", None),
        "gift_name": r.gift_name,
        "timestamp": (r.sent_at.isoformat() if getattr(r, "sent_at", None) else None),
        "position": getattr(r, "position", None),
    }

_BUCKETS = {"10s": 10, "30s": 30, "1m": 60, "5m": 300}

# ---------- main endpoint ----------
@router.get(
    "/gift-timeline/{stream_id}",
    summary="Gift timeline (filters + pagination + ETag + optional bucketing)"
)
def get_gift_timeline(
    stream_id: int,
    response: Response,
    db: Session = Depends(get_db),
    # filters
    since: Optional[datetime] = Query(None, description="ISO start time"),
    until: Optional[datetime] = Query(None, description="ISO end time"),
    min_pos: Optional[float] = Query(None, ge=0),
    max_pos: Optional[float] = Query(None, ge=0),
    gifts: Optional[List[str]] = Query(None, description="Filter by gift_name (multi)"),
    since_id: Optional[int] = Query(None, ge=1, description="Incremental: only id > since_id"),
    # output/paging
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    compact: bool = Query(True, description="True=payload ndogo kwa mobile"),
    bucket: str = Query("raw", pattern="^(raw|10s|30s|1m|5m)$",
                        description="Downsample for charts (raw or bucketed)"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    if not GiftFly:
        raise HTTPException(status_code=500, detail="GiftFly model haijapatikana")

    q = db.query(GiftFly).filter(GiftFly.stream_id == stream_id)

    if since:
        q = q.filter(GiftFly.sent_at >= since)
    if until:
        q = q.filter(GiftFly.sent_at <= until)
    if min_pos is not None:
        q = q.filter(GiftFly.position >= float(min_pos))
    if max_pos is not None:
        q = q.filter(GiftFly.position <= float(max_pos))
    if since_id is not None and hasattr(GiftFly, "id"):
        q = q.filter(GiftFly.id > since_id)
    if gifts:
        # CASE-insensitive like → badilisha kama unataka exact match
        ors = [GiftFly.gift_name.ilike(g) if "%" in g else (GiftFly.gift_name == g) for g in gifts]
        if ors:
            q = q.filter(and_(*ors)) if len(ors) == 1 else q.filter(or_(*ors))  # type: ignore[name-defined]

    # mpangilio
    order_col = getattr(GiftFly, "sent_at", getattr(GiftFly, "id"))
    q = q.order_by(order_col.asc() if order == "asc" else order_col.desc())

    # raw mode (default): paginate at DB
    if bucket == "raw":
        total = q.count()
        rows = q.offset(offset).limit(limit).all()
        tag = _etag(rows, extra=f"{stream_id}|raw|{offset}|{limit}|{order}")
        if if_none_match and if_none_match == tag:
            return Response(status_code=304)
        response.headers["ETag"] = tag
        response.headers["Cache-Control"] = "public, max-age=5"
        response.headers["X-Total-Count"] = str(total)
        response.headers["X-Limit"] = str(limit)
        response.headers["X-Offset"] = str(offset)
        serializer = _compact_row if compact else _full_row
        return [serializer(r) for r in rows]

    # bucketed mode: leta hadi N kubwa kidogo, kisha group kwa sekunde/bucket
    raw = q.limit(min(limit * 10, 20000)).all()
    tag = _etag(raw, extra=f"{stream_id}|{bucket}")
    if if_none_match and if_none_match == tag:
        return Response(status_code=304)
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "public, max-age=10"

    bucket_s = _BUCKETS[bucket]
    # group in python (rahisi, portable)
    from collections import defaultdict
    series = defaultdict(lambda: {"count": 0, "by_gift": defaultdict(int)})
    for r in raw:
        if not getattr(r, "sent_at", None):
            continue
        ts = int(r.sent_at.timestamp())
        bstart = ts - (ts % bucket_s)
        key = datetime.fromtimestamp(bstart, tz=timezone.utc).isoformat()
        series[key]["count"] += 1
        series[key]["by_gift"][r.gift_name] += 1

    items = []
    for k in sorted(series.keys()):
        entry = series[k]
        # compact au full
        if compact:
            items.append({"t": k, "n": entry["count"], "g": dict(entry["by_gift"])})
        else:
            items.append({"bucket_start": k, "total": entry["count"], "by_gift": dict(entry["by_gift"])})
    # rudisha na optional limit/offset kwa bucketed pia
    return items[offset: offset + limit]
