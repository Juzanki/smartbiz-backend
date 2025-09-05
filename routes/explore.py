from __future__ import annotations
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional, List, Any

from fastapi import APIRouter, Depends, Query, Response, HTTPException, status, Header
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.db import get_db

# ==================== Schemas ====================
# Jaribu kutumia schema halisi; ukikosa, tumia fallback hapa chini.
try:
    from backend.schemas.explore_schema import LiveStreamExploreOut
    _HAS_EXTERNAL_SCHEMA = True
except Exception:
    _HAS_EXTERNAL_SCHEMA = False

if not _HAS_EXTERNAL_SCHEMA:
    # ---- Pydantic v2/v1 compatibility header ----
    try:
        from pydantic import BaseModel, Field, ConfigDict
        _P2 = True
    except Exception:  # v1 fallback
        from pydantic import BaseModel, Field  # type: ignore
        ConfigDict = dict  # type: ignore
        _P2 = False

    class LiveStreamExploreOut(BaseModel):
        id: int
        title: str = Field(..., min_length=1, max_length=200)
        thumbnail_url: Optional[str] = None
        is_live: bool = False
        viewers: int = 0
        tags: Optional[List[str]] = None
        started_at: Optional[datetime] = None

        # Pydantic config (use ONE style depending on version)
        if _P2:
            model_config = ConfigDict(from_attributes=True)
        else:
            class Config:  # type: ignore
                orm_mode = True

# ==================== Models ====================
try:
    from backend.models.live_stream import LiveStream
except Exception as e:
    raise RuntimeError("?? Missing model: backend.models.LiveStream") from e

router = APIRouter(prefix="/explore", tags=["Explore"])

# ===================== Helpers =====================
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _best_ts(m: Any) -> datetime:
    return getattr(m, "updated_at", None) or getattr(m, "started_at", None) or _utcnow()

def _etag_from_rows(rows: List[Any]) -> str:
    if not rows:
        base = "empty"
    else:
        ids = ",".join(str(getattr(r, "id", 0)) for r in rows[:100])  # cap
        last_ts = max((_best_ts(r) for r in rows), default=_utcnow())
        base = f"{ids}|{last_ts.isoformat()}"
    return 'W/"' + hashlib.sha256(base.encode("utf-8")).hexdigest()[:16] + '"'

def _serialize_many(rows: List[Any]) -> List[LiveStreamExploreOut]:
    out: List[LiveStreamExploreOut] = []
    # Pydantic v2 ina classmethod model_validate; v1 tumia from_orm
    if hasattr(LiveStreamExploreOut, "model_validate"):
        for r in rows:
            out.append(LiveStreamExploreOut.model_validate(r, from_attributes=True))  # v2
    else:
        for r in rows:
            out.append(LiveStreamExploreOut.from_orm(r))  # v1
    return out

def _apply_common_filters(q, *,
                          is_live: bool = True,
                          language: Optional[str] = None,
                          category: Optional[str] = None,
                          country: Optional[str] = None,
                          min_viewers: Optional[int] = None,
                          since: Optional[datetime] = None):
    if is_live and hasattr(LiveStream, "is_live"):
        q = q.filter(LiveStream.is_live.is_(True))
    if language and hasattr(LiveStream, "language"):
        q = q.filter(LiveStream.language == language)
    if category and hasattr(LiveStream, "category"):
        q = q.filter(LiveStream.category == category)
    if country and hasattr(LiveStream, "country"):
        q = q.filter(LiveStream.country == country)
    if min_viewers is not None and hasattr(LiveStream, "viewers_count"):
        q = q.filter(LiveStream.viewers_count >= int(min_viewers))
    if since and hasattr(LiveStream, "started_at"):
        q = q.filter(LiveStream.started_at >= since)
    return q

# ===================== Featured =====================
@router.get(
    "/featured",
    response_model=List[LiveStreamExploreOut],
    summary="Featured live streams (paged + filters + ETag/304)"
)
def get_featured_streams(
    response: Response,
    db: Session = Depends(get_db),
    # filters
    language: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    min_viewers: Optional[int] = Query(None, ge=0),
    since: Optional[datetime] = Query(None, description="ISO datetime; return streams started after this"),
    # paging
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    # caching
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    q = db.query(LiveStream)
    if hasattr(LiveStream, "is_featured"):
        q = q.filter(LiveStream.is_featured.is_(True))

    q = _apply_common_filters(
        q,
        is_live=True,
        language=language,
        category=category,
        country=country,
        min_viewers=min_viewers,
        since=since,
    )

    # Order: most recent featured first (fallback to id)
    order_col = getattr(LiveStream, "started_at", getattr(LiveStream, "id"))
    q = q.order_by(order_col.desc())

    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    etag = _etag_from_rows(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=20"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    return _serialize_many(rows)

# ===================== Trending =====================
def _hybrid_score(v: int, g: int, started_at: Optional[datetime]) -> float:
    """
    Hybrid scoring:
      score = (viewers*10 + gifts*3) * decay(hours_since_start)
      decay(t) = 0.8 ** t
    """
    v = max(0, v or 0)
    g = max(0, g or 0)
    age_h = 0.0
    if started_at:
        age_h = max(0.0, (_utcnow() - started_at).total_seconds() / 3600.0)
    base = v * 10.0 + g * 3.0
    return base * (0.8 ** age_h)

@router.get(
    "/trending",
    response_model=List[LiveStreamExploreOut],
    summary="Trending live streams (hybrid algorithm + filters + pagination + ETag/304)"
)
def get_trending_streams(
    response: Response,
    db: Session = Depends(get_db),
    # filters
    language: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    min_viewers: Optional[int] = Query(1, ge=0),
    since: Optional[datetime] = Query(None),
    # algo
    algo: str = Query("hybrid", description="hybrid|viewers|gifts|recent"),
    pool: int = Query(400, ge=50, le=2000, description="Candidate pool before scoring"),
    # paging
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    # caching
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    # 1) Candidate query (cheap ordering to pull a good pool)
    q = db.query(LiveStream)
    q = _apply_common_filters(
        q,
        is_live=True,
        language=language,
        category=category,
        country=country,
        min_viewers=min_viewers,
        since=since,
    )

    # cheap pre-sort to capture likely trending candidates
    viewers = getattr(LiveStream, "viewers_count", None)
    gifts = getattr(LiveStream, "gifts_count", None)
    started = getattr(LiveStream, "started_at", None)

    order_cols = []
    if algo in {"hybrid", "viewers"} and viewers is not None:
        order_cols.append(viewers.desc())
    if algo in {"hybrid", "gifts"} and gifts is not None:
        order_cols.append(gifts.desc())
    if started is not None:
        order_cols.append(started.desc())

    if order_cols:
        q = q.order_by(*order_cols)

    # NB: total = all live (with filters), not just pool
    total = q.count()

    # pull candidate pool
    cand = q.limit(pool + offset + limit).all()

    # 2) Score in Python (portable & controllable)
    def score_row(r: Any) -> float:
        if algo == "viewers":
            return float(getattr(r, "viewers_count", 0) or 0)
        if algo == "gifts":
            return float(getattr(r, "gifts_count", 0) or 0)
        if algo == "recent":
            st = getattr(r, "started_at", None)
            # more recent ? higher (use negative ts for ascending sort)
            return -(_best_ts(r).timestamp()) if st else 0.0
        # hybrid
        return _hybrid_score(
            getattr(r, "viewers_count", 0) or 0,
            getattr(r, "gifts_count", 0) or 0,
            getattr(r, "started_at", None),
        )

    # stable sort by score then id (avoid shuffle)
    cand_sorted = sorted(
        cand,
        key=lambda r: (score_row(r), getattr(r, "id", 0)),
        reverse=True,
    )

    rows = cand_sorted[offset: offset + limit]

    etag = _etag_from_rows(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=15"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    return _serialize_many(rows)