# backend/routes/nfc_orders.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import hashlib
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.db import get_db
from backend.models.product import Product
router = APIRouter(prefix="/nfc", tags=["NFC Orders"])

# ---- NFC ID validation ----
HEX_RE = re.compile(r"^[A-Fa-f0-9]{6,64}$")
UUID_RE = re.compile(r"^[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[1-5][A-Fa-f0-9]{3}-[89ABab][A-Fa-f0-9]{3}-[A-Fa-f0-9]{12}$")

def _normalize_tag(tag_id: str) -> str:
    if not tag_id:
        raise HTTPException(status_code=422, detail="tag_id is required")
    s = tag_id.strip()
    if HEX_RE.match(s) or UUID_RE.match(s):
        return s.lower()
    # allow simple demo IDs: letters/digits/-_. between 4 and 128 chars
    if 4 <= len(s) <= 128 and all(c.isalnum() or c in "-_." for c in s):
        return s.lower()
    raise HTTPException(status_code=422, detail="Invalid tag_id format")

def _etag_for(product: Product) -> str:
    basis = "|".join([
        f"id:{getattr(product, 'id', '')}",
        f"upd:{getattr(product, 'updated_at', '')}",
        f"price:{getattr(product, 'price', '')}",
        f"stock:{getattr(product, 'stock', '')}",
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()

# ---- Schemas ----
class ProductByNfcOut(BaseModel):
    product_id: int
    name: str
    description: Optional[str] = None
    price: float | int
    stock: int | None = None
    image: Optional[str] = Field(None, alias="image_url")
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

# ---- Endpoint ----
@router.get("/product-info", response_model=ProductByNfcOut, summary="Fetch product by NFC tag")
def get_product_by_nfc(
    request: Request,
    response: Response,
    tag_id: str = Query(..., description="Unique NFC tag ID"),
    db: Session = Depends(get_db),
):
    """
    Resolve an NFC tag to a product. Case-insensitive match on Product.nfc_tag.
    Returns 304 if If-None-Match matches current ETag.
    """
    norm = _normalize_tag(tag_id)

    # Case-insensitive lookup; prefer functional index on lower(nfc_tag)
    product = (
        db.query(Product)
        .filter(func.lower(Product.nfc_tag) == norm)  # requires Product.nfc_tag column
        .first()
    )
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # ETag support for efficient polling/caching
    etag = _etag_for(product)
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"

    return ProductByNfcOut.model_validate(product)

