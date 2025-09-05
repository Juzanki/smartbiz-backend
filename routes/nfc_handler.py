# backend/routes/nfc_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import hashlib
import re
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from sqlalchemy import case

from backend.db import get_db

# Optional models â€“ router works even if these donâ€™t exist yet
try:
    from backend.models.nfc_tag import NfcTag  # tag_id:str, product_id:int, is_active:bool, updated_at:dt
except Exception:  # pragma: no cover
    NfcTag = None  # type: ignore

try:
from backend.models.product import Product
except Exception:  # pragma: no cover
    Product = None  # type: ignore

try:
    from backend.models.nfc_scan import NfcScan  # tag_id, product_id, client, scanned_at
except Exception:  # pragma: no cover
    NfcScan = None  # type: ignore


router = APIRouter(prefix="/nfc", tags=["NFC"])

HEX_RE = re.compile(r"^[A-Fa-f0-9]{6,64}$")   # flexible for typical tag payloads
UUID_RE = re.compile(r"^[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[1-5][A-Fa-f0-9]{3}-[89ABab][A-Fa-f0-9]{3}-[A-Fa-f0-9]{12}$")

UTC_NOW = lambda: datetime.now(timezone.utc)

# ---------- Schemas ----------
class ProductBrief(BaseModel):
    id: int
    name: str
    price: float | int
    image: Optional[str] = Field(None, alias="image_url")
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

class NFCReadOut(BaseModel):
    status: str = Field(..., example="ok")
    nfc_id: str
    mapped: bool
    next_step: str
    product: Optional[ProductBrief] = None
    model_config = ConfigDict(from_attributes=True)

# ---------- Helpers ----------
def _normalize_nfc_id(raw: str) -> str:
    if not raw:
        raise HTTPException(status_code=422, detail="nfc_id is required")
    s = raw.strip()
    if HEX_RE.match(s) or UUID_RE.match(s):
        return s.lower()
    # Allow short demo IDs but still validate
    if len(s) >= 4 and len(s) <= 128 and all(c.isalnum() or c in "-_." for c in s):
        return s
    raise HTTPException(status_code=422, detail="Invalid nfc_id format")

def _etag_for(live_id: str, product: Optional[object], tag_updated_at: Optional[datetime]) -> str:
    basis = "|".join([
        f"nfc:{live_id}",
        f"p:{getattr(product, 'id', '')}",
        f"pu:{getattr(product, 'updated_at', None) or ''}",
        f"tu:{tag_updated_at or ''}",
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()

def _log_scan(db: Session, tag_id: str, product_id: Optional[int], client: Optional[str]) -> None:
    if not NfcScan:
        return
    try:
        rec = NfcScan(tag_id=tag_id, product_id=product_id, client=client, scanned_at=UTC_NOW())
        db.add(rec)
        db.commit()
    except Exception:
        db.rollback()  # best-effort logging only

# ---------- Routes ----------
@router.get(
    "/{nfc_id}",
    response_model=NFCReadOut,
    summary="Handle NFC tag scan and resolve to product (if mapped)",
)
def read_nfc(
    nfc_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    include_product: bool = Query(True, description="Include product details if the tag is mapped"),
    preserve_order: bool = Query(True, description="Preserve order of selected IDs if your mapping stores a list"),
    client: Optional[str] = Query(None, description="Client identifier (device id/app id) for analytics"),
):
    tag_id = _normalize_nfc_id(nfc_id)

    product_obj = None
    tag_updated_at: Optional[datetime] = None

    if NfcTag and Product:
        tag = (
            db.query(NfcTag)
              .filter(NfcTag.tag_id == tag_id, getattr(NfcTag, "is_active", True) == True)  # noqa: E712
              .first()
        )
        if tag and include_product and getattr(tag, "product_id", None):
            q = db.query(Product).filter(Product.id == tag.product_id)
            # If you ever support an ordered list of mapped products, keep deterministic ordering
            if preserve_order and isinstance(getattr(tag, "product_ids", None), list):
                ids: List[int] = [int(i) for i in tag.product_ids if str(i).isdigit()]
                if ids:
                    from sqlalchemy import case as _case
                    q = db.query(Product).filter(Product.id.in_(ids)).order_by(
                        _case(value=Product.id, whens={pid: idx for idx, pid in enumerate(ids)})
                    )
                    product_obj = q.first()  # pick first as primary
                else:
                    product_obj = q.first()
            else:
                product_obj = q.first()
            tag_updated_at = getattr(tag, "updated_at", None)

    # ETag for efficient polling
    etag = _etag_for(tag_id, product_obj, tag_updated_at)
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"

    # Best-effort scan logging (non-blocking for client UX)
    try:
        _log_scan(db, tag_id, getattr(product_obj, "id", None), client)
    except Exception:
        pass

    if product_obj:
        return NFCReadOut(
            status="ok",
            nfc_id=tag_id,
            mapped=True,
            next_step="Show product details",
            product=ProductBrief.model_validate(product_obj),
        )

    # Preview/fallback when no mapping exists
    return NFCReadOut(
        status="preview",
        nfc_id=tag_id,
        mapped=False,
        next_step="Scan successful, but no product mapping found",
        product=None,
    )

