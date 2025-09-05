from __future__ import annotations
# backend/routes/bot_packages.py
import json
import hashlib
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from backend.db import get_db

# Model import (support both layouts)
try:
    from backend.models.bot_package import BotPackage as BotPackageModel
except Exception:  # pragma: no cover
    from backend.models.bot_package import BotPackage as BotPackageModel

# Schema import; toa fallback ndogo kama haipo
try:
    from backend.schemas.bot_package_schemas import BotPackageOut
except Exception:  # pragma: no cover
    from pydantic import BaseModel
    class BotPackageOut(BaseModel):  # minimal fallback
        id: int
        name: str
        slug: str | None = None
        description: str | None = None
        price: float | int | None = None
        currency: str | None = None
        billing_cycle: str | None = None
        is_active: bool | None = None
        features: Dict[str, Any] | List[Any] | None = None
        created_at: datetime | None = None
        updated_at: datetime | None = None

router = APIRouter(prefix="/bot-packages", tags=["Bot Packages"])

# ---------- Helpers ----------
ALLOWED_SORT = ("created_at", "updated_at", "price", "id", "name")
ALLOWED_ORDER = ("asc", "desc")
MAX_LIMIT = 200

def _clamp_limit(limit: Optional[int], default: int = 50) -> int:
    if not limit:
        return default
    return max(1, min(int(limit), MAX_LIMIT))

def _parse_features(v) -> Any:
    """
    Badili features kuwa dict/list bila kubadilisha ORM instance:
    - Ikiwa ni string -> jaribu json.loads
    - Ikiwa tayari ni dict/list -> rudisha kama ilivyo
    - Ikiwa None -> rudisha {} kwa urahisi wa UI
    """
    if v is None:
        return {}
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, (bytes, bytearray)):
        try:
            return json.loads(v.decode("utf-8"))
        except Exception:
            return {}
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return {}
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v

def _to_out(obj: BotPackageModel) -> BotPackageOut:
    # Chuja SQLAlchemy internals
    attrs = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    # Hakikisha features ni JSON tayari
    attrs["features"] = _parse_features(attrs.get("features"))
    # Pydantic v2/v1 support
    if hasattr(BotPackageOut, "model_validate"):
        return BotPackageOut.model_validate(attrs)  # type: ignore
    return BotPackageOut(**attrs)  # type: ignore

def _compute_list_etag(items: List[BotPackageModel]) -> str:
    if not items:
        return 'W/"empty"'
    parts = []
    for it in items:
        uid = getattr(it, "id", None)
        upd = getattr(it, "updated_at", None)
        ts = ""
        if isinstance(upd, datetime):
            ts = str(int(upd.replace(tzinfo=timezone.utc).timestamp()))
        parts.append(f"{uid}-{ts}")
    raw = "|".join(parts)
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f'W/"{h}"'

def _order_by_whitelist(model, sort_by: str, order: str):
    key = sort_by if sort_by in ALLOWED_SORT else "created_at"
    col = getattr(model, key)
    return col.asc() if order == "asc" else col.desc()

# ---------- Endpoints ----------
@router.get(
    "/",
    response_model=List[BotPackageOut],
    summary="Orodha ya bot packages (search, filter, paginate, sort)"
)
def get_all_bot_packages(
    response: Response,
    db: Session = Depends(get_db),
    # Search & filters
    q: Optional[str] = Query(None, description="Tafuta kwa jina/maelezo"),
    active: Optional[bool] = Query(None, description="Chuja kwa is_active"),
    billing_cycle: Optional[str] = Query(None, description="mf. monthly, yearly"),
    # Sort & paginate
    sort_by: str = Query("created_at", description=f"Sort key: {', '.join(ALLOWED_SORT)}"),
    order: str = Query("desc", description=f"Order: {', '.join(ALLOWED_ORDER)}"),
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    with_count: bool = Query(False, description="Onyesha jumla kwa header X-Total-Count"),
):
    limit = _clamp_limit(limit)
    qy = db.query(BotPackageModel)

    if q:
        like = f"%{q.strip()}%"
        conds = []
        for field in ("name", "description"):
            if hasattr(BotPackageModel, field):
                conds.append(getattr(BotPackageModel, field).ilike(like))
        if conds:
            qy = qy.filter(or_(*conds))

    if active is not None and hasattr(BotPackageModel, "is_active"):
        qy = qy.filter(BotPackageModel.is_active == bool(active))

    if billing_cycle and hasattr(BotPackageModel, "billing_cycle"):
        qy = qy.filter(BotPackageModel.billing_cycle == billing_cycle)

    qy = qy.order_by(_order_by_whitelist(BotPackageModel, sort_by, order))

    total = None
    if with_count:
        total = qy.with_entities(func.count(BotPackageModel.id)).scalar() or 0

    items = qy.offset(offset).limit(limit).all()
    outs = [_to_out(it) for it in items]

    # Mobile-friendly headers
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    if total is not None:
        response.headers["X-Total-Count"] = str(total)
    response.headers["ETag"] = _compute_list_etag(items)

    return outs


@router.get(
    "/{id_or_slug}",
    response_model=BotPackageOut,
    summary="Pata package moja kwa ID au slug"
)
def get_bot_package(
    id_or_slug: str,
    response: Response,
    db: Session = Depends(get_db),
):
    qy = db.query(BotPackageModel)
    item = None
    # Jaribu ID ya namba kwanza
    try:
        iid = int(id_or_slug)
        item = qy.filter(BotPackageModel.id == iid).first()
    except Exception:
        # Jaribu slug kama column ipo
        if hasattr(BotPackageModel, "slug"):
            item = qy.filter(BotPackageModel.slug == id_or_slug).first()

    if not item:
        raise HTTPException(status_code=404, detail="Bot package not found")

    response.headers["Cache-Control"] = "no-store"
    if hasattr(item, "updated_at"):
        ts = getattr(item, "updated_at", None)
        base = str(int(ts.replace(tzinfo=timezone.utc).timestamp())) if isinstance(ts, datetime) else str(item.id)
        response.headers["ETag"] = f'W/"{base}"'

    return _to_out(item)

