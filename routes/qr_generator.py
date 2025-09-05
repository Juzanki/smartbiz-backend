from __future__ import annotations
# backend/routes/qr_code.py
# âœ¨ QR Codes for Products â€” PNG au JSON (base64), ETag/304, UTM, customization
import os
import io
import base64
import hashlib
from typing import Optional
from urllib.parse import urljoin, urlencode, urlparse, parse_qsl, urlunparse

from fastapi import (
    APIRouter, HTTPException, Response, Query, Header, Depends, status
)
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session
import qrcode
from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q, ERROR_CORRECT_H

# (hiari) kama unataka kuthibitisha product ipo kabla ya kutengeneza QR
try:
    from backend.db import get_db
    from backend.models.product import Product
    _HAS_DB = True
except Exception:
    _HAS_DB = False
    def get_db():  # type: ignore
        return None  # placeholder

router = APIRouter(prefix="/qr", tags=["QR Codes"])

# --------------------------- Helpers ---------------------------

def _public_base_url() -> str:
    """
    Chukua URL ya public API/app. Unaweza kutumia moja wapo ya hizi kwenye .env:
      - RAILWAY_PUBLIC_URL / NETLIFY_PUBLIC_URL / VITE_API_URL
    Fallback: https://yourdomain.com
    """
    for k in ("RAILWAY_PUBLIC_URL", "NETLIFY_PUBLIC_URL", "VITE_API_URL"):
        v = os.getenv(k)
        if v and v.startswith("http"):
            return v.rstrip("/")
    return "https://yourdomain.com"

def _product_url(product_id: int, *, utm_source: Optional[str], utm_medium: Optional[str], utm_campaign: Optional[str]) -> str:
    base = _public_base_url()
    url = urljoin(base + "/", f"product/{product_id}")
    if any([utm_source, utm_medium, utm_campaign]):
        u = urlparse(url)
        q = dict(parse_qsl(u.query))
        if utm_source:   q["utm_source"] = utm_source
        if utm_medium:   q["utm_medium"] = utm_medium
        if utm_campaign: q["utm_campaign"] = utm_campaign
        url = urlunparse(u._replace(query=urlencode(q)))
    return url

def _validate_hex(color: str) -> str:
    c = color.strip()
    if c.startswith("#"):
        c = c[1:]
    if len(c) not in (3, 6) or any(ch not in "0123456789abcdefABCDEF" for ch in c):
        raise HTTPException(status_code=400, detail=f"Invalid color: {color}. Use hex like #000 or #000000")
    if len(c) == 3:  # expand #abc -> #aabbcc
        c = "".join(ch*2 for ch in c)
    return "#" + c.lower()

def _qr_png(url: str, *, size: int, level: str, margin: int, fg: str, bg: str) -> bytes:
    ec_map = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "Q": ERROR_CORRECT_Q, "H": ERROR_CORRECT_H}
    ec = ec_map.get(level.upper(), ERROR_CORRECT_M)

    # box_size: ukubwa wa dot; tunakisia kwa uwiano thabiti na size (512px -> box ~12)
    box = max(2, min(40, size // 42))
    qr = qrcode.QRCode(
        version=None,  # auto fit
        error_correction=ec,
        box_size=box,
        border=max(0, min(16, margin)),
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color=fg, back_color=bg)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def _etag_for(product_id: int, url: str, size: int, level: str, margin: int, fg: str, bg: str, fmt: str) -> str:
    base = f"{product_id}|{url}|{size}|{level}|{margin}|{fg}|{bg}|{fmt}"
    return 'W/"' + hashlib.sha256(base.encode()).hexdigest()[:16] + '"'

# --------------------------- Endpoints ---------------------------

@router.get(
    "/product/{product_id}",
    summary="Generate QR code for a product (PNG or JSON base64)"
)
def product_qr(
    product_id: int,
    response: Response,
    fmt: str = Query("png", pattern="^(png|json)$", description="png=raw image, json=base64"),
    size: int = Query(512, ge=96, le=2048, description="Image size in pixels"),
    level: str = Query("M", pattern="^(L|M|Q|H)$", description="QR error correction"),
    margin: int = Query(4, ge=0, le=16, description="Quiet zone (modules)"),
    fg: str = Query("#000000", description="Foreground color hex"),
    bg: str = Query("#FFFFFF", description="Background color hex"),
    utm_source: Optional[str] = Query(None),
    utm_medium: Optional[str] = Query(None),
    utm_campaign: Optional[str] = Query(None),
    verify_exists: bool = Query(False, description="Ikiwa True, hakikisha product ipo"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
    db: Session = Depends(get_db),
):
    if product_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid product_id")

    # (hiari) hakikisha product ipo
    if verify_exists and _HAS_DB:
        exists = db.query(Product.id).filter(Product.id == product_id).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Product not found")

    # sanitize rangi
    fg = _validate_hex(fg)
    bg = _validate_hex(bg)

    url = _product_url(product_id, utm_source=utm_source, utm_medium=utm_medium, utm_campaign=utm_campaign)
    etag = _etag_for(product_id, url, size, level, margin, fg, bg, fmt)

    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)

    png_bytes = _qr_png(url, size=size, level=level, margin=margin, fg=fg, bg=bg)

    # Cache headers (tzuri kwa mobile)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=86400"  # 1 day

    if fmt == "png":
        return StreamingResponse(io.BytesIO(png_bytes), media_type="image/png")

    # fmt == json
    b64 = base64.b64encode(png_bytes).decode("ascii")
    payload = {
        "product_id": product_id,
        "link": url,
        "qr_code_base64": b64,
        "size": size,
        "level": level,
        "margin": margin,
        "fg": fg,
        "bg": bg,
    }
    return JSONResponse(payload)

@router.head(
    "/product/{product_id}",
    summary="HEAD: pata tu ETag kwa QR iliyotarajiwa (kwa client cache validation)"
)
def head_product_qr(
    product_id: int,
    size: int = Query(512, ge=96, le=2048),
    level: str = Query("M", pattern="^(L|M|Q|H)$"),
    margin: int = Query(4, ge=0, le=16),
    fg: str = Query("#000000"),
    bg: str = Query("#FFFFFF"),
    utm_source: Optional[str] = None,
    utm_medium: Optional[str] = None,
    utm_campaign: Optional[str] = None,
):
    fg = _validate_hex(fg)
    bg = _validate_hex(bg)
    url = _product_url(product_id, utm_source=utm_source, utm_medium=utm_medium, utm_campaign=utm_campaign)
    etag = _etag_for(product_id, url, size, level, margin, fg, bg, "png")
    return Response(status_code=204, headers={"ETag": etag, "Cache-Control": "public, max-age=86400"})

# --------------------------- Utility (export) ---------------------------
def generate_product_qr(data: str, *, size: int = 512, level: str = "M", margin: int = 4,
                        fg: str = "#000000", bg: str = "#FFFFFF", as_base64: bool = True) -> str | bytes:
    """
    ðŸ”§ Utility unayoweza kuitumia sehemu zingine:
    - as_base64=True  => str (base64 PNG)
    - as_base64=False => bytes (raw PNG)
    """
    fg = _validate_hex(fg); bg = _validate_hex(bg)
    png = _qr_png(data, size=size, level=level, margin=margin, fg=fg, bg=bg)
    return base64.b64encode(png).decode("ascii") if as_base64 else png

