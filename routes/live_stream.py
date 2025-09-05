# backend/routes/live_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import json
import hashlib
from typing import List, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from sqlalchemy import case

from backend.db import get_db
from backend.models.live_session import LiveSession
from backend.models.product import Product
router = APIRouter(prefix="/live", tags=["Live"])

# ---------- Schemas (Pydantic v2) ----------
class ProductBriefOut(BaseModel):
    id: int
    name: str
    price: float | int
    image: Optional[str] = Field(None, alias="image_url")
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

class LiveOut(BaseModel):
    id: int
    title: Optional[str] = None
    category: Optional[str] = None
    started_at: Optional[str] = None  # FastAPI will serialize datetime -> ISO8601
    user_id: Optional[int] = None
    model_config = ConfigDict(from_attributes=True)

class LiveCurrentOut(BaseModel):
    live: LiveOut
    products: List[ProductBriefOut] = []
    model_config = ConfigDict(from_attributes=True)

# ---------- Helpers ----------
def _normalize_selected_products(raw) -> List[int]:
    """
    Accepts list[int]/list[str] or a JSON-encoded string; returns a de-duplicated list[int] preserving order.
    """
    ids: List[int] = []
    if not raw:
        return ids

    candidates: Sequence = []
    if isinstance(raw, (list, tuple)):
        candidates = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
            if isinstance(data, (list, tuple)):
                candidates = data
        except Exception:
            candidates = []

    seen = set()
    for v in candidates:
        try:
            pid = int(v)
        except Exception:
            continue
        if pid not in seen:
            seen.add(pid)
            ids.append(pid)
    return ids

# ---------- Routes ----------
@router.get(
    "/current",
    response_model=LiveCurrentOut,
    summary="Get the currently active live session (with optional products)",
)
def get_current_live(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    include_products: bool = Query(True, description="Include selected products in the response"),
    product_limit: int = Query(12, ge=1, le=50, description="Max products to return if included"),
    preserve_order: bool = Query(True, description="Preserve the selected_products order"),
):
    # 1) Find the most recent active live session
    live = (
        db.query(LiveSession)
        .filter(LiveSession.active.is_(True))
        .order_by(LiveSession.started_at.desc())
        .first()
    )
    if not live:
        raise HTTPException(status_code=404, detail="No active live session")

    # 2) Optionally fetch product data
    products: List[Product] = []
    product_ids: List[int] = []

    if include_products and getattr(live, "selected_products", None):
        product_ids = _normalize_selected_products(live.selected_products)[:product_limit]

        if product_ids:
            q = db.query(Product).filter(Product.id.in__(product_ids))
            if preserve_order:
                # ORDER BY the explicit ids order
                order_case = case(value=Product.id, whens={pid: idx for idx, pid in enumerate(product_ids)})
                q = q.order_by(order_case)
            products = q.all()

    # 3) Compute an ETag so clients can cache efficiently
    etag_basis = "|".join(
        [
            f"id:{getattr(live, 'id', '')}",
            f"started:{getattr(live, 'started_at', '')}",
            f"pcount:{len(products)}",
            f"pids:{','.join(map(str, product_ids))}",
        ]
    )
    etag = hashlib.sha256(etag_basis.encode("utf-8")).hexdigest()
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"

    # 4) Build the response
    live_payload = LiveOut.model_validate(live)
    product_payload = [ProductBriefOut.model_validate(p) for p in products]

    return LiveCurrentOut(live=live_payload, products=product_payload)

