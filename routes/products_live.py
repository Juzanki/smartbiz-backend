from __future__ import annotations
# backend/routes/products_live.py
import hashlib
from typing import Optional, List, Any
from datetime import datetime, timezone
from contextlib import suppress

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Header, Response, Path
)
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from backend.db import get_db
from backend.models.products_live import LiveProduct
from backend.schemas.products_live_schema import LiveProductCreate, LiveProductOut

# (Hiari) Auth â€“ kama ipo tutaitumia kulinda multi-tenant
with suppress(Exception):
    from backend.auth import get_current_user  # returns user object

# (Hiari) Thibitisha product inayoingizwa ipo
ProductModel = None
with suppress(Exception):
    from backend.models.product import Product as ProductModel

router = APIRouter(prefix="/products-live", tags=["Live Products"])

# ================= Helpers =================
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _etag_rows(rows: List[Any]) -> str:
    if not rows:
        return 'W/"empty"'
    last = max(
        getattr(r, "updated_at", None)
        or getattr(r, "added_at", None)
        or getattr(r, "created_at", None)
        or datetime.min
        for r in rows
    )
    ids = ",".join(str(getattr(r, "id", 0)) for r in rows[:200])
    base = f"{ids}|{last.isoformat()}"
    return 'W/"' + hashlib.sha256(base.encode()).hexdigest()[:16] + '"'

def _serialize_many(rows: List[Any]) -> List[LiveProductOut]:
    out: List[LiveProductOut] = []
    for r in rows:
        if hasattr(LiveProductOut, "model_validate"):  # pydantic v2
            out.append(LiveProductOut.model_validate(r, from_attributes=True))
        else:  # pydantic v1
            out.append(LiveProductOut.model_validate(r))
    return out

# ================= Create (idempotent upsert) =================
@router.post(
    "",
    response_model=LiveProductOut,
    status_code=status.HTTP_201_CREATED,
    summary="Ongeza bidhaa kwenye live room (idempotent kwa room_id+product_id)"
)
def add_product_to_live(
    data: LiveProductCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user) if "get_current_user" in globals() else None,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    # (Hiari) Multi-tenant scoping: kama LiveProduct ina owner_id/host_id, jaribu kulinda
    with suppress(Exception):
        # kama schema yako ina user/owner kwenye LiveProductCreate na haukuweka, weka ya current_user
        if current_user is not None and hasattr(LiveProduct, "owner_id"):
            if getattr(data, "owner_id", None) is None:
                setattr(data, "owner_id", getattr(current_user, "id", None))

    # (Hiari) hakikisha product ipo
    if ProductModel:
        prod = db.query(ProductModel).filter(ProductModel.id == data.product_id).first()
        if not prod:
            raise HTTPException(status_code=404, detail="Product not found")

    # Idempotent: kama tayari ipo rekodi kwa room_id+product_id â†’ rudi hiyo hiyo
    existing = (
        db.query(LiveProduct)
        .filter(
            LiveProduct.room_id == data.room_id,
            LiveProduct.product_id == data.product_id,
        )
        .first()
    )
    if existing:
        response.headers["Cache-Control"] = "no-store"
        return LiveProductOut.model_validate(existing, from_attributes=True) if hasattr(LiveProductOut, "model_validate") else LiveProductOut.model_validate(existing)

    row = LiveProduct(**data.dict())
    # timestamps (kama zipo)
    if hasattr(row, "added_at") and getattr(row, "added_at", None) is None:
        row.added_at = _utcnow()
    if hasattr(row, "created_at") and getattr(row, "created_at", None) is None:
        row.created_at = _utcnow()
    if hasattr(row, "updated_at"):
        row.updated_at = _utcnow()

    db.add(row)
    db.commit()
    db.refresh(row)

    response.headers["Cache-Control"] = "no-store"
    return LiveProductOut.model_validate(row, from_attributes=True) if hasattr(LiveProductOut, "model_validate") else LiveProductOut.model_validate(row)

# ================= Bulk add =================
from pydantic import BaseModel
class BulkAdd(BaseModel):
    room_id: str
    product_ids: List[int]

@router.post(
    "/bulk",
    response_model=List[LiveProductOut],
    summary="Ongeza bidhaa nyingi kwa mara moja (idempotent per product)"
)
def bulk_add_products(
    payload: BulkAdd,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user) if "get_current_user" in globals() else None,
):
    room_id = payload.room_id
    out: List[LiveProductOut] = []

    # chukua zilizopo tayari ili tuepuke duplicate inserts
    existing_map = {
        lp.product_id: lp
        for lp in db.query(LiveProduct)
        .filter(LiveProduct.room_id == room_id, LiveProduct.product_id.in_(payload.product_ids))
        .all()
    }

    # (Hiari) hakikisha products zipo
    valid_ids = set(payload.product_ids)
    if ProductModel:
        got_ids = {
            r.id for r in db.query(ProductModel.id).filter(ProductModel.id.in_(payload.product_ids)).all()
        }
        missing = valid_ids - got_ids
        if missing:
            # tuna-skip zisizopo badala ya kufaili zote
            valid_ids = got_ids

    for pid in valid_ids:
        if pid in existing_map:
            lp = existing_map[pid]
            out.append(
                LiveProductOut.model_validate(lp, from_attributes=True)
                if hasattr(LiveProductOut, "model_validate")
                else LiveProductOut.model_validate(lp)
            )
            continue
        row = LiveProduct(room_id=room_id, product_id=pid)
        if hasattr(row, "added_at"):
            row.added_at = _utcnow()
        if hasattr(row, "created_at"):
            row.created_at = _utcnow()
        if hasattr(row, "updated_at"):
            row.updated_at = _utcnow()
        db.add(row)
        db.flush()  # pata id kabla ya commit
        out.append(
            LiveProductOut.model_validate(row, from_attributes=True)
            if hasattr(LiveProductOut, "model_validate")
            else LiveProductOut.model_validate(row)
        )

    db.commit()
    return out

# ================= List (paged + search + sorting + ETag) =================
@router.get(
    "/{room_id}",
    response_model=List[LiveProductOut],
    summary="Orodha ya bidhaa za live room (pagination + sorting + search + ETag/304)"
)
def get_live_products(
    room_id: str,
    response: Response,
    db: Session = Depends(get_db),
    q: Optional[str] = Query(None, description="tafuta kwa jina (hutumia join ya haraka kama Product ipo)"),
    sort_by: str = Query("added_at", description="added_at|updated_at|price|popularity"),
    order: str = Query("desc", regex="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    qry = db.query(LiveProduct).filter(LiveProduct.room_id == room_id)

    # search (ikiwa Product ipo, fanya join ya haraka)
    if q and ProductModel:
        like = f"%{q}%"
        qry = (
            qry.join(ProductModel, ProductModel.id == LiveProduct.product_id)
            .filter(or_(ProductModel.name.ilike(like),
                        ProductModel.description.ilike(like)))
        )
    elif q:
        # bila join: tunaweza kuchuja kwa field ya LiveProduct ikiwa ipo (mf. cached_product_name)
        with suppress(Exception):
            if hasattr(LiveProduct, "product_name"):
                qry = qry.filter(LiveProduct.product_name.ilike(f"%{q}%"))

    # sorting whitelist
    # kidokezo: kama una fields hizi (price/popularity) kwenye LiveProduct au una view yenyeo
    col = None
    if sort_by == "updated_at" and hasattr(LiveProduct, "updated_at"):
        col = LiveProduct.updated_at
    elif sort_by == "added_at" and hasattr(LiveProduct, "added_at"):
        col = LiveProduct.added_at
    elif sort_by == "price" and hasattr(LiveProduct, "price"):
        col = LiveProduct.price
    elif sort_by == "popularity" and hasattr(LiveProduct, "popularity"):
        col = LiveProduct.popularity
    else:
        col = getattr(LiveProduct, "id")  # fallback

    qry = qry.order_by(col.asc() if order == "asc" else col.desc())

    total = qry.count()
    rows = qry.offset(offset).limit(limit).all()

    etag = _etag_rows(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=10"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    return _serialize_many(rows)

# ================= Remove (pair-aware) =================
@router.delete(
    "/{room_id}/{product_id}",
    response_model=dict,
    summary="Ondoa bidhaa kwenye live room (kwa room_id + product_id)"
)
def remove_product(
    room_id: str,
    product_id: int,
    db: Session = Depends(get_db)
):
    live_product = (
        db.query(LiveProduct)
        .filter(LiveProduct.room_id == room_id, LiveProduct.product_id == product_id)
        .first()
    )
    if not live_product:
        raise HTTPException(status_code=404, detail="Product not found in live room")
    db.delete(live_product)
    db.commit()
    return {"detail": "Product removed"}

# ----- (Backward compatibility) toa kwa product_id tu â€” hutumia ya kwanza tu -----
@router.delete(
    "/{product_id}",
    include_in_schema=False  # tujifiche kwenye docs; tumia route mpya iliyo juu
)
def remove_product_legacy(product_id: int, db: Session = Depends(get_db)):
    live_product = db.query(LiveProduct).filter(LiveProduct.product_id == product_id).first()
    if not live_product:
        raise HTTPException(status_code=404, detail="Product not found in any live room")
    db.delete(live_product)
    db.commit()
    return {"detail": "Product removed (legacy endpoint)"}


