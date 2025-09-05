from __future__ import annotations
# backend/routes/fans.py
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Any, Dict
from contextlib import suppress

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Header, Response
)
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.db import get_db
from backend.schemas.fan import FanCreate, FanOut
from backend.crud import fan_crud  # tunaleta kama primary path

# Auth (hiari; tukikosa, route itaendelea kufanya kazi bila ku-force login)
with suppress(Exception):
    from backend.auth import get_current_user  # -> returns current user object

# Model (hiari kwa fallback ya direct ORM)
FanModel = None
with suppress(Exception):
    from backend.models.fan import Fan as FanModel  # type: ignore

router = APIRouter(prefix="/fans", tags=["Fans"])

# =================== Helpers ===================
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _etag_from_rows(rows: List[Any]) -> str:
    if not rows:
        base = "empty"
    else:
        ids = ",".join(str(getattr(r, "id", 0)) for r in rows[:100])
        # tumia max(updated_at/last_seen/created_at) kama signature
        def ts(x):
            return (
                getattr(x, "updated_at", None)
                or getattr(x, "last_seen", None)
                or getattr(x, "created_at", None)
                or datetime.min
            )
        last_ts = max((ts(r) for r in rows), default=datetime.min)
        base = f"{ids}|{last_ts.isoformat()}"
    return 'W/"' + hashlib.sha256(base.encode("utf-8")).hexdigest()[:16] + '"'

def _serialize_many(rows: List[Any]) -> List[FanOut]:
    out: List[FanOut] = []
    for r in rows:
        if hasattr(FanOut, "model_validate"):  # pydantic v2
            out.append(FanOut.model_validate(r, from_attributes=True))
        else:  # pydantic v1
            out.append(FanOut.model_validate(r))
    return out

def _require_same_user(fan: FanCreate, current_user) -> None:
    """
    Ikiwa schema yako ina `user_id`, hakikisha haiandikii kwa user mwingine (salama kwa mobile).
    Kama haina `user_id`, ignore.
    """
    uid = getattr(fan, "user_id", None)
    if current_user is not None and uid is not None and uid != getattr(current_user, "id", None):
        raise HTTPException(status_code=403, detail="Cannot create/update fan for another user")

# =================== Create/Update (Idempotent) ===================
@router.post(
    "/",
    response_model=FanOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create or update fan (idempotent, mobile-friendly)"
)
def create_or_update_fan(
    fan: FanCreate,
    response: Response,
    db: Session = Depends(get_db),
    # auth ni hiari â€” ukiwa na get_current_user, itatumika
    current_user=Depends(get_current_user) if "get_current_user" in globals() else None,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    - Hutumia `fan_crud.create_or_update_fan` kama ipo (recommended).
    - Idempotency ya kimsingi: kama rekodi haijabadilika, rudisha 200/201 na data ile ile.
    - Ikiwa schema ina `user_id`, inalindwa dhidi ya kuandika kwa mtu mwingine.
    """
    _require_same_user(fan, current_user)

    # (Hiari) weka user_id kutoka auth kama schema ina field na hajatuma
    if current_user is not None and hasattr(fan, "user_id") and getattr(fan, "user_id", None) is None:
        setattr(fan, "user_id", getattr(current_user, "id", None))

    # Tumia CRUD kama ipo (ndiyo njia bora â€“ inategemewa kuwa na upsert)
    if hasattr(fan_crud, "create_or_update_fan"):
        row = fan_crud.create_or_update_fan(db, fan, idempotency_key=idempotency_key)  # idempotency param ni hiari kwa CRUD yako
    else:
        # Fallback ya ORM: jaribu ku-upsert kwa (host_id, user_id) au (host_id, handle)
        if not FanModel:
            raise HTTPException(status_code=500, detail="Fan storage not configured")

        # Hapa tunatafuta unique key kwa akili ya kawaida
        q = db.query(FanModel).filter(FanModel.host_id == getattr(fan, "host_id"))
        if hasattr(fan, "user_id") and getattr(fan, "user_id", None) is not None:
            q = q.filter(FanModel.user_id == getattr(fan, "user_id"))
        elif hasattr(fan, "handle") and getattr(fan, "handle", None):
            q = q.filter(FanModel.handle == getattr(fan, "handle"))
        existing = q.first()

        if existing:
            # update fields zilizoletwa
            for k, v in fan.dict(exclude_unset=True).items():
                setattr(existing, k, v)
            if hasattr(existing, "updated_at"):
                existing.updated_at = _utcnow()
            db.commit()
            db.refresh(existing)
            row = existing
        else:
            row = FanModel(**fan.dict())
            if hasattr(row, "created_at"):
                row.created_at = _utcnow()
            if hasattr(row, "updated_at"):
                row.updated_at = _utcnow()
            db.add(row)
            db.commit()
            db.refresh(row)

    # Headers za UX
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = _etag_from_rows([row])
    return FanOut.model_validate(row, from_attributes=True) if hasattr(FanOut, "model_validate") else FanOut.model_validate(row)

# =================== Top Fans (paged + filters + ETag) ===================
@router.get(
    "/top/{host_id}",
    response_model=List[FanOut],
    summary="Top fans kwa host (pagination, sorting, timeframe, ETag)"
)
def get_top_fans(
    host_id: int,
    response: Response,
    db: Session = Depends(get_db),
    # filters
    since: Optional[str] = Query(None, description="timeframe: 24h|7d|30d|all (default: all)"),
    min_score: Optional[int] = Query(None, ge=0, description="chujia wafuasi wenye alama ndogo"),
    # sorting
    sort_by: str = Query("score", description="score|coins|gifts|interactions|last_seen|created_at"),
    order: str = Query("desc", regex="^(asc|desc)$"),
    # paging
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    # caching
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    # Kwanza jaribu CRUD advanced, kisha fallback
    if hasattr(fan_crud, "get_top_fans_advanced"):
        rows, total = fan_crud.get_top_fans_advanced(
            db=db,
            host_id=host_id,
            since=since,
            min_score=min_score,
            sort_by=sort_by,
            order=order,
            limit=limit,
            offset=offset,
        )
    else:
        # Fallback ORM generic
        if not FanModel:
            # fallback ya zamani: tumia CRUD ya zamani kisha paginate hapa
            if hasattr(fan_crud, "get_top_fans"):
                base = fan_crud.get_top_fans(db, host_id) or []
                total = len(base)
                # rudimentary sort in-memory
                def keyf(x):
                    # pendelea fields zinazojulikana, fallback â†’ score/coins/gifts/interactions
                    for attr in [sort_by, "score", "coins", "gifts", "interactions", "last_seen", "created_at", "id"]:
                        if hasattr(x, attr):
                            return getattr(x, attr) or 0
                    return 0
                base = sorted(base, key=keyf, reverse=(order == "desc"))
                rows = base[offset: offset + limit]
            else:
                raise HTTPException(status_code=500, detail="Fan listing not configured")
        else:
            q = db.query(FanModel).filter(FanModel.host_id == host_id)

            # timeframe
            if since and since.lower() in {"24h", "7d", "30d"}:
                hours = {"24h": 24, "7d": 7 * 24, "30d": 30 * 24}[since.lower()]
                cutoff = _utcnow() - timedelta(hours=hours)
                # kutumia last_seen/updated_at ikiwa zipo
                if hasattr(FanModel, "last_seen"):
                    q = q.filter(FanModel.last_seen >= cutoff)
                elif hasattr(FanModel, "updated_at"):
                    q = q.filter(FanModel.updated_at >= cutoff)

            if min_score is not None and hasattr(FanModel, "score"):
                q = q.filter(FanModel.score >= int(min_score))

            # sorting whitelist
            candidates: Dict[str, Any] = {
                "score": getattr(FanModel, "score", None),
                "coins": getattr(FanModel, "coins", None),
                "gifts": getattr(FanModel, "gifts", None),
                "interactions": getattr(FanModel, "interactions", None),
                "last_seen": getattr(FanModel, "last_seen", None),
                "created_at": getattr(FanModel, "created_at", None),
                "id": getattr(FanModel, "id", None),
            }
            sort_col = candidates.get(sort_by) or candidates.get("score") or candidates["id"]
            q = q.order_by(sort_col.asc() if order == "asc" else sort_col.desc())

            total = q.count()
            rows = q.offset(offset).limit(limit).all()

    # Caching-friendly headers
    etag = _etag_from_rows(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=20"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    # Serialize
    return _serialize_many(rows)

