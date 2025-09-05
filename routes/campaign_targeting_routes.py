from __future__ import annotations
# backend/routes/campaign_targeting.py
import os
import csv
import json
import time
import random
from io import StringIO
from typing import List, Optional, Dict, Any, Iterable

from fastapi import (
    APIRouter, Depends, HTTPException, Query, Response, Header, status
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.models.customer import Customer
from backend.schemas.targeting import TargetingCriteria

# Targeting engine (best effort)
try:
    from backend.utils.targeting_engine import (
        filter_customers,
        count_customers as _count_customers,
        filter_customers_explain as _filter_explain,  # optional
    )
except Exception:
    # minimal fallback if some symbols are missing
    from backend.utils.targeting_engine import filter_customers  # type: ignore
    _count_customers = None
    _filter_explain = None

router = APIRouter(prefix="/campaign", tags=["Campaign Targeting"])

# ------------------------- Config & limits ------------------------- #
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
ALLOWED_SORT = ("id", "created_at", "updated_at", "last_active_at", "lifetime_value")
ALLOWED_ORDER = ("asc", "desc")
TARGETING_RATE_PER_MIN = int(os.getenv("TARGETING_RATE_PER_MIN", "20"))

_RATE: Dict[int, List[float]] = {}  # per-user sliding window

def _rate_ok(user_id: int) -> None:
    now = time.time()
    q = _RATE.setdefault(user_id, [])
    while q and (now - q[0]) > 60.0:
        q.pop(0)
    if len(q) >= TARGETING_RATE_PER_MIN:
        raise HTTPException(status_code=429, detail="Too many targeting requests. Try again shortly.")
    q.append(now)

def _clamp_limit(limit: Optional[int]) -> int:
    if not limit:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))

def _order_by_whitelist(model, sort_by: str, order: str):
    key = sort_by if sort_by in ALLOWED_SORT else "id"
    col = getattr(model, key) if hasattr(model, key) else getattr(model, "id")
    return col.asc() if order == "asc" else col.desc()

# ------------------------- Projection / serialization ------------------------- #
# Jaribu kutumia schema yako ya CustomerOut kama ipo; la sivyo fallback nyepesi
try:
    from backend.schemas.customer import CustomerOut as CustomerPreviewOut
    def _to_out(obj: Customer, fields: Optional[List[str]] = None) -> Any:
        data = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
        if fields:
            data = {k: data.get(k) for k in fields if k in data}
        # Pydantic v1/v2
        if hasattr(CustomerPreviewOut, "model_validate"):
            return CustomerPreviewOut.model_validate(data)  # type: ignore
        return CustomerPreviewOut(**data)  # type: ignore
except Exception:
    from pydantic import BaseModel
    class CustomerPreviewOut(BaseModel):
        id: int
        name: Optional[str] = None
        email: Optional[str] = None
        phone: Optional[str] = None
        language: Optional[str] = None
        city: Optional[str] = None
        plan: Optional[str] = None
        created_at: Optional[str] = None
        updated_at: Optional[str] = None
        last_active_at: Optional[str] = None
        lifetime_value: Optional[float] = None
    def _to_out(obj: Customer, fields: Optional[List[str]] = None) -> Any:
        # toa attrs salama bila SQLA internals
        raw = {
            "id": getattr(obj, "id", None),
            "name": getattr(obj, "name", None) or getattr(obj, "full_name", None),
            "email": getattr(obj, "email", None),
            "phone": getattr(obj, "phone", None) or getattr(obj, "phone_number", None),
            "language": getattr(obj, "language", None),
            "city": getattr(obj, "city", None),
            "plan": getattr(obj, "plan", None) or getattr(obj, "subscription_status", None),
            "created_at": getattr(obj, "created_at", None),
            "updated_at": getattr(obj, "updated_at", None),
            "last_active_at": getattr(obj, "last_active_at", None),
            "lifetime_value": getattr(obj, "lifetime_value", None),
        }
        if fields:
            raw = {k: raw.get(k) for k in fields if k in raw}
        if hasattr(CustomerPreviewOut, "model_validate"):
            return CustomerPreviewOut.model_validate(raw)  # type: ignore
        return CustomerPreviewOut(**raw)  # type: ignore

# ------------------------- Helpers ------------------------- #
def _project_fields_param(fields: Optional[str]) -> Optional[List[str]]:
    """
    Ruksa kuja kama: fields="id,name,phone"
    """
    if not fields:
        return None
    arr = [f.strip() for f in fields.split(",") if f.strip()]
    return arr or None

def _cursor_next(items: List[Customer]) -> Optional[int]:
    if not items:
        return None
    return int(getattr(items[-1], "id", 0)) or None

# ======================= COUNT ONLY (fast) ======================= #
@router.post("/target-count", summary="Hesabu idadi ya wateja wanaolingana na vigezo")
def count_targeted_customers(
    criteria: TargetingCriteria,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _rate_ok(current_user.id)

    # Jaribu kutumia engine ya haraka endapo ipo
    if _count_customers:
        try:
            return {"count": int(_count_customers(db, criteria))}
        except Exception:
            pass

    # Fallback: tumia filter halafu uhesabu
    try:
        rows = filter_customers(db, criteria)
        return {"count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Count failed: {e}")

# ======================= FACETS (for UI filters) ======================= #
@router.post("/target-facets", summary="Takwimu za makundi (mf. language/plan/city)")
def target_facets(
    criteria: TargetingCriteria,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _rate_ok(current_user.id)
    # Path ya haraka: kama engine ina "filter_query" rudisha query ya SQLAlchemy (advanced)
    q = None
    try:
        from backend.utils.targeting_engine import filter_query  # type: ignore
        q = filter_query(db, criteria)
    except Exception:
        pass

    def _facet_counts(qry, col_name: str) -> Dict[str, int]:
        if not hasattr(Customer, col_name):
            return {}
        col = getattr(Customer, col_name)
        data = qry.with_entities(col, func.count(Customer.id)).group_by(col).all()
        return {str(k) if k is not None else "null": int(v) for k, v in data}

    if q is None:
        # fallback: pata sample iliyochujwa kisha jihesabie (si sahihi kwa set kubwa)
        rows = filter_customers(db, criteria)
        def _local(name):
            if not rows or not hasattr(rows[0], name):
                return {}
            out: Dict[str, int] = {}
            for r in rows:
                key = getattr(r, name, None)
                key = str(key) if key is not None else "null"
                out[key] = out.get(key, 0) + 1
            return out
        return {
            "language": _local("language"),
            "plan": _local("plan"),
            "city": _local("city"),
        }

    return {
        "language": _facet_counts(q, "language"),
        "plan": _facet_counts(q, "plan"),
        "city": _facet_counts(q, "city"),
    }

# ======================= PREVIEW (paged + sampling) ======================= #
@router.post(
    "/target-preview",
    response_model=List[CustomerPreviewOut],
    summary="Orodha ya wateja wanaolingana (preview, pagination, sorting, sampling)"
)
def preview_targeted_customers(
    criteria: TargetingCriteria,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    # Pagination & sorting
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0, description="Ignored if cursor provided"),
    cursor: Optional[int] = Query(None, description="ID-based cursor (preferable for mobile infinite scroll)"),
    sort_by: str = Query("id", description=f"Sort key: {', '.join(ALLOWED_SORT)}"),
    order: str = Query("desc", description=f"Order: {', '.join(ALLOWED_ORDER)}"),
    with_count: bool = Query(False),
    # Sampling
    sample: Optional[int] = Query(None, ge=1, le=MAX_LIMIT, description="Random sample from the result (applied after filters)"),
    seed: Optional[int] = Query(None, description="Random seed for stable sampling"),
    # Projection
    fields: Optional[str] = Query(None, description="Comma-separated fields to project (id,name,phone,email,...)"),
    # Explain (ikiwa engine inaunga mkono)
    explain: bool = Query(False, description="Ongeza sababu za kulinganisha (ikiwa targeting_engine ina support)"),
):
    _rate_ok(current_user.id)
    fields_list = _project_fields_param(fields)
    limit = _clamp_limit(limit)

    # Path 1: jaribu kupata SQLAlchemy query halisi (ikiwa engine yako ina filter_query)
    q = None
    try:
        from backend.utils.targeting_engine import filter_query  # type: ignore
        q = filter_query(db, criteria)
    except Exception:
        pass

    total = None
    rows: List[Customer] = []

    if q is not None:
        # Apply sorting
        q = q.order_by(_order_by_whitelist(Customer, sort_by, order))
        # Cursor pagination
        if cursor and hasattr(Customer, "id"):
            if order == "desc":
                q = q.filter(Customer.id < cursor)
            else:
                q = q.filter(Customer.id > cursor)
            rows = q.limit(limit).all()
            offset_used = 0
        else:
            rows = q.offset(offset).limit(limit).all()
            offset_used = offset

        if with_count:
            try:
                total = q.with_entities(func.count(Customer.id)).scalar() or 0
            except Exception:
                total = None
    else:
        # Path 2: fallback — tumia filter_customers halafu kata kwa mkono
        try:
            rows_all = filter_customers(db, criteria)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Filtering failed: {e}")

        if with_count:
            total = len(rows_all)

        # Sorting (in-Python if no query)
        keymap = {
            "id": lambda r: getattr(r, "id", 0),
            "created_at": lambda r: getattr(r, "created_at", None) or 0,
            "updated_at": lambda r: getattr(r, "updated_at", None) or 0,
            "last_active_at": lambda r: getattr(r, "last_active_at", None) or 0,
            "lifetime_value": lambda r: getattr(r, "lifetime_value", 0.0),
        }
        keyfun = keymap.get(sort_by, keymap["id"])
        rows_all.sort(key=keyfun, reverse=(order == "desc"))

        if cursor:
            # apply simple cursor on id
            def _after_cursor(r):  # type: ignore
                rid = getattr(r, "id", 0) or 0
                return rid < cursor if order == "desc" else rid > cursor
            sliced = [r for r in rows_all if _after_cursor(r)]
            rows = sliced[:limit]
            offset_used = 0
        else:
            rows = rows_all[offset : offset + limit]
            offset_used = offset

    # Optional: random sampling from current page result
    if sample and rows:
        rnd = random.Random(seed) if seed is not None else random
        rows = rnd.sample(rows, min(sample, len(rows)))

    # Explain support (best effort)
    explanations: Dict[int, Any] = {}
    if explain and _filter_explain:
        try:
            explanations = _filter_explain(db, criteria, rows)  # expect {customer_id: {...}}
        except Exception:
            explanations = {}

    # Serialize
    data = [_to_out(r, fields_list) for r in rows]
    if explain and explanations:
        # Ambatanisha 'explain' kwenye dict ya response (ikiwa schema yako inaruhusu; la sivyo, weka header tu)
        try:
            # Pydantic model → dict, ongeza explain kisha re-wrap
            payload: List[Dict[str, Any]] = []
            for item in data:
                d = item.dict() if hasattr(item, "dict") else dict(item)
                d["_explain"] = explanations.get(d.get("id"))
                payload.append(d)
            data = payload  # type: ignore
        except Exception:
            # Toa bendera ya kuwa explanations zinapatikana
            response.headers["X-Explain-Attached"] = "false"

    # Headers for mobile/infinite scroll
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset_used)
    next_cur = _cursor_next(rows) if order == "desc" else None
    if next_cur:
        response.headers["X-Cursor-Next"] = str(next_cur)
    if total is not None:
        response.headers["X-Total-Count"] = str(total)

    return data

# ======================= EXPORT (CSV/NDJSON) ======================= #
@router.post(
    "/target-export",
    summary="Export ya walengwa (CSV/NDJSON, streaming)",
    status_code=status.HTTP_200_OK,
)
def export_targeted_customers(
    criteria: TargetingCriteria,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    fmt: str = Query("ndjson", pattern="ndjson|csv"),
    fields: Optional[str] = Query(None, description="id,name,email,phone,..."),
    limit: int = Query(100000, ge=1, le=1_000_000),
):
    _rate_ok(current_user.id)
    # Jaribu query ya moja kwa moja
    q = None
    try:
        from backend.utils.targeting_engine import filter_query  # type: ignore
        q = filter_query(db, criteria)
    except Exception:
        pass

    fields_list = _project_fields_param(fields)

    # Pata data (bounded by limit)
    if q is not None:
        rows = q.order_by(Customer.id.asc()).limit(limit).all()
    else:
        rows = filter_customers(db, criteria)[:limit]

    if fmt == "ndjson":
        def gen():
            for r in rows:
                item = _to_out(r, fields_list)
                d = item.dict() if hasattr(item, "dict") else dict(item)
                yield (json.dumps(d, ensure_ascii=False) + "\n").encode("utf-8")
        return StreamingResponse(gen(), media_type="application/x-ndjson")

    # CSV
    # Bainisha vichwa vya CSV
    if fields_list:
        headers = fields_list
    else:
        headers = ["id", "name", "email", "phone", "language", "city", "plan", "created_at", "updated_at", "last_active_at", "lifetime_value"]

    def gen_csv():
        buf = StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            item = _to_out(r, fields_list)
            d = item.dict() if hasattr(item, "dict") else dict(item)
            writer.writerow(d)
        yield buf.getvalue()

    return StreamingResponse(gen_csv(), media_type="text/csv; charset=utf-8")
