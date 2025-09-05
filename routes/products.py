from __future__ import annotations
# backend/routes/products_search.py
import hashlib
import math
from typing import Optional, List, Any, Dict
from contextlib import suppress
from datetime import datetime, timezone

from fastapi import (
    APIRouter, Depends, Query, Header, Response, HTTPException, status
)
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from backend.db import get_db
from backend.auth import get_current_user

# --------- Schemas (tumia zako; hizi ni fallback zikikosekana) -------------
try:
    from backend.schemas import ProductOut
except Exception:
    from pydantic import BaseModel
    class ProductOut(BaseModel):
        id: int
        name: str
        description: Optional[str] = None
        category: Optional[str] = None
        price: Optional[float] = None
        in_stock: Optional[bool] = None
        updated_at: Optional[datetime] = None
        created_at: Optional[datetime] = None
        class Config:
            orm_mode = True
        model_config = {"from_attributes": True}

# --------- Model ------------------------------------------------------------
try:
    from backend.models.product import Product
except Exception as e:
    raise RuntimeError("âš ï¸ Missing model: backend.models.Product") from e

router = APIRouter(prefix="/products", tags=["Products"])

# ======================= Helpers =======================
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _etag_rows(rows: List[Any]) -> str:
    if not rows:
        return 'W/"empty"'
    ids = ",".join(str(getattr(r, "id", 0)) for r in rows[:200])
    last = max(
        getattr(r, "updated_at", None) or getattr(r, "created_at", None) or datetime.min
        for r in rows
    )
    base = f"{ids}|{last.isoformat()}"
    return 'W/"' + hashlib.sha256(base.encode()).hexdigest()[:16] + '"'

def _serialize_many(rows: List[Any]) -> List[ProductOut]:
    out: List[ProductOut] = []
    for r in rows:
        if hasattr(ProductOut, "model_validate"):  # pydantic v2
            out.append(ProductOut.model_validate(r, from_attributes=True))
        else:  # pydantic v1
            out.append(ProductOut.model_validate(r))
    return out

def _py_score(row: Any, q_tokens: List[str]) -> float:
    """
    Relevance ya haraka upande wa app iwapo DB haina FTS/pg_trgm.
    Uzito: name*3 + category*1.5 + description*1
    """
    name = (getattr(row, "name", "") or "").lower()
    cat  = (getattr(row, "category", "") or "").lower()
    desc = (getattr(row, "description", "") or "").lower()
    score = 0.0
    for t in q_tokens:
        if t in name: score += 3.0
        if t in cat:  score += 1.5
        if t in desc: score += 1.0
    # boost for in_stock / price popularity if fields zipo
    if getattr(row, "in_stock", None):
        score *= 1.05
    pop = getattr(row, "popularity", None)
    if isinstance(pop, (int, float)) and pop > 0:
        score *= (1.0 + min(pop, 1000) / 2000.0)
    return score

# ======================= SEARCH =======================
@router.get(
    "/search",
    response_model=List[ProductOut],
    summary="ðŸ” Auto Search Products (paged + filters + sorting + ETag)"
)
def search_products(
    response: Response,
    q: str = Query(..., min_length=2, description="Search keyword"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),

    # Filters (hiari)
    category: Optional[str] = Query(None),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    in_stock: Optional[bool] = Query(None),
    mine_only: bool = Query(True, description="Chuja kwa bidhaa za account hii (multi-tenant)"),

    # Sorting
    sort_by: str = Query("relevance", description="relevance|price|created_at|updated_at|popularity"),
    order: str = Query("desc", regex="^(asc|desc)$"),

    # Pagination
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),

    # Caching
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    q_norm = q.strip()
    if not q_norm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty query")

    # 1) Base query + security scoping
    qry = db.query(Product)

    # scope kwa tenant: Product.owner_id lazima ilindwe kama ipo
    if mine_only and hasattr(Product, "owner_id"):
        qry = qry.filter(Product.owner_id == getattr(current_user, "id", None))

    # 2) Basic filters
    if category and hasattr(Product, "category"):
        qry = qry.filter(func.lower(Product.category) == category.lower())
    if min_price is not None and hasattr(Product, "price"):
        qry = qry.filter(Product.price >= min_price)
    if max_price is not None and hasattr(Product, "price"):
        qry = qry.filter(Product.price <= max_price)
    if in_stock is not None and hasattr(Product, "in_stock"):
        qry = qry.filter(Product.in_stock.is_(bool(in_stock)))

    # 3) Search: jaribu DB-side kwanza (ILIKE); ukipata FTS/pg_trgm, unaweza kupanua
    like = f"%{q_norm}%"
    parts = []
    if hasattr(Product, "name"):        parts.append(Product.name.ilike(like))
    if hasattr(Product, "description"): parts.append(Product.description.ilike(like))
    if hasattr(Product, "category"):    parts.append(Product.category.ilike(like))
    if parts:
        qry = qry.filter(or_(*parts))

    total = qry.count()

    # 4) Sorting
    #   - relevance: tutasort app-side (Python) kwa usahihi zaidi
    #   - vingine: DB order_by
    if sort_by != "relevance":
        col_map = {
            "price":      getattr(Product, "price", None),
            "created_at": getattr(Product, "created_at", None),
            "updated_at": getattr(Product, "updated_at", None),
            "popularity": getattr(Product, "popularity", None),  # kama una field hii
        }
        col = col_map.get(sort_by)
        if col is None:
            # fallback
            col = getattr(Product, "updated_at", getattr(Product, "id"))
        qry = qry.order_by(col.asc() if order == "asc" else col.desc())
        rows = qry.offset(offset).limit(limit).all()
    else:
        # Relevance ranking upande wa app (portable na mwepesi)
        # Tuna-chota pool kubwa kidogo ili relevance iwe na maana
        pool_size = min(max(limit * 4, 80), 400)
        pre_sorted = qry.order_by(
            # cheap hints
            getattr(Product, "updated_at", getattr(Product, "id")).desc()
        ).limit(pool_size + offset + limit).all()

        tokens = [t for t in q_norm.lower().split() if t]
        ranked = sorted(pre_sorted, key=lambda r: (_py_score(r, tokens), getattr(r, "id", 0)), reverse=True)
        rows = ranked[offset: offset + limit]

    # 5) ETag / 304
    etag = _etag_rows(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=15"

    # 6) Paging headers
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    return _serialize_many(rows)


