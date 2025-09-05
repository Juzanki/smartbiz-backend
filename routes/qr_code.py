from __future__ import annotations
# backend/routes/qr_code.py
import os
import io
import base64
import hashlib
from typing import Optional
from contextlib import suppress
from urllib.parse import urljoin, urlencode, urlparse, parse_qsl, urlunparse

from fastapi import APIRouter, HTTPException, Query, Header, Response, Depends, status
from sqlalchemy.orm import Session

# (hiari) verify product exists
with suppress(Exception):
    from backend.db import get_db  # type: ignore
with suppress(Exception):
from backend.models.product import Product

# ---- primary util (preferred) ------------------------------------------------
qr_generate = None
with suppress(Exception):
    from backend.utils.qr_generator import generate_product_qr as _gen  # type: ignore
    qr_generate = _gen

# ---- fallback util (optional) ------------------------------------------------
def _fallback_qr_png(url: str, size: int, level: str, margin: int, fg: str, bg: str) -> bytes:
    """
    Jaribu fallback ukikosa util yako. Inahitaji `qrcode` lib ikiwa ipo.
    Inarudisha PNG bytes.
    """
    with suppress(Exception):
        import qrcode
        from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q, ERROR_CORRECT_H

        ec_map = {
            "L": ERROR_CORRECT_L,
            "M": ERROR_CORRECT_M,
            "Q": ERROR_CORRECT_Q,
            "H": ERROR_CORRECT_H,
        }
        ec = ec_map.get(level.upper(), ERROR_CORRECT_M)

        qr = qrcode.QRCode(
            version=None,  # auto
            error_correction=ec,
            box_size=max(1, size // 40),  # approx sizing
            border=max(0, margin),
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color=fg, back_color=bg)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    raise RuntimeError("QR generator not available: provide backend.utils.qr_generator or install `qrcode`.")

# ------------------------------------------------------------------------------
router = APIRouter(prefix="/qr", tags=["QR Codes"])

def _base_public_url() -> str:
    # chukua URL ya public kutoka env; rudisha fallback salama
    for key in ("RAILWAY_PUBLIC_URL", "NETLIFY_PUBLIC_URL", "VITE_API_URL"):
        v = os.getenv(key)
        if v and v.startswith("http"):
            return v.rstrip("/")
    return "https://smartbiz.com"

def _product_url(product_id: int, *, utm_source: Optional[str], utm_medium: Optional[str],
                 utm_campaign: Optional[str]) -> str:
    base = _base_public_url()
    # hakikisha path inakuwa /products/<id>
    url = urljoin(base + "/", f"products/{product_id}")
    # ongeza UTM kama zimeombwa
    if any([utm_source, utm_medium, utm_campaign]):
        u = urlparse(url)
        q = dict(parse_qsl(u.query))
        if utm_source:   q["utm_source"] = utm_source
        if utm_medium:   q["utm_medium"] = utm_medium
        if utm_campaign: q["utm_campaign"] = utm_campaign
        url = urlunparse(u._replace(query=urlencode(q)))
    return url

def _etag_for(product_id: int, url: str, size: int, level: str, margin: int, fg: str, bg: str, fmt: str) -> str:
    base = f"{product_id}|{url}|{size}|{level}|{margin}|{fg}|{bg}|{fmt}"
    return 'W/"' + hashlib.sha256(base.encode()).hexdigest()[:16] + '"'

# ============================== Routes =======================================

@router.get(
    "/product/{product_id}",
    summary="Generate QR code for product (JSON base64 or PNG)",
)
def get_product_qr(
    product_id: int,
    response: Response,
    # output & style
    fmt: str = Query("json", pattern="^(json|png)$", description="json=base64, png=raw image"),
    size: int = Query(512, ge=64, le=2048, description="Image size in pixels"),
    level: str = Query("M", pattern="^(L|M|Q|H)$", description="Error correction level"),
    margin: int = Query(4, ge=0, le=16, description="Quiet zone in modules"),
    fg: str = Query("#000000", description="Foreground (hex)"),
    bg: str = Query("#FFFFFF", description="Background (hex)"),
    # UTM (optional)
    utm_source: Optional[str] = Query(None),
    utm_medium: Optional[str] = Query(None),
    utm_campaign: Optional[str] = Query(None),
    # perf
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
    # security/consistency
    verify_exists: bool = Query(False, description="Ikiwa True, hakikisha product ipo kabla ya kutoa QR"),
    db: Session = Depends(get_db) if "get_db" in globals() else None,
):
    if product_id <= 0:
        raise HTTPException(status_code=400, detail="Product ID is invalid")

    # (hiari) thibitisha product ipo
    if verify_exists and "Product" in globals() and db is not None:
        exists = db.query(Product.id).filter(Product.id == product_id).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Product not found")

    product_url = _product_url(product_id, utm_source=utm_source, utm_medium=utm_medium, utm_campaign=utm_campaign)

    etag = _etag_for(product_id, product_url, size, level, margin, fg, bg, fmt)
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)

    # tengeneza QR
    png_bytes: Optional[bytes] = None
    b64_str: Optional[str] = None

    if qr_generate:
        # Jaribu kuunga mkono saini mpana; fallback kwa minimal
        with suppress(Exception):
            # signature ya kisasa (ikikuwapo)
            out = qr_generate(
                url=product_url,
                size=size,
                level=level,
                margin=margin,
                fg=fg,
                bg=bg,
                as_base64=(fmt == "json"),
            )
            if isinstance(out, bytes):
                png_bytes = out
            elif isinstance(out, str):
                # tukipata base64 tu, tutaitumia
                b64_str = out
        if png_bytes is None and b64_str is None:
            # util yako ya zamani: inaweza kurudisha base64 pekee
            with suppress(Exception):
                b64_str = qr_generate(product_url)  # type: ignore
    # fallback lib
    if png_bytes is None and fmt == "png":
        png_bytes = _fallback_qr_png(product_url, size, level, margin, fg, bg)
    if b64_str is None and fmt == "json":
        # kama tuna png_bytes, i-base64
        if png_bytes is None:
            png_bytes = _fallback_qr_png(product_url, size, level, margin, fg, bg)
        b64_str = base64.b64encode(png_bytes).decode("ascii")

    # headers za cache (PNG ni cacheable zaidi)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=86400"  # siku 1

    if fmt == "png":
        assert png_bytes is not None
        response.media_type = "image/png"
        return Response(content=png_bytes, media_type="image/png")
    else:
        assert b64_str is not None
        return {
            "product_id": product_id,
            "link": product_url,
            "qr_code_base64": b64_str,
            "size": size,
            "level": level,
            "margin": margin,
            "fg": fg,
            "bg": bg,
        }

@router.head("/product/{product_id}", include_in_schema=False)
def head_product_qr(
    product_id: int,
    size: int = Query(512, ge=64, le=2048),
    level: str = Query("M", pattern="^(L|M|Q|H)$"),
    margin: int = Query(4, ge=0, le=16),
    fg: str = Query("#000000"),
    bg: str = Query("#FFFFFF"),
    utm_source: Optional[str] = None,
    utm_medium: Optional[str] = None,
    utm_campaign: Optional[str] = None,
):
    # rudisha tu ETag ili client ajue kama apakue tena
    product_url = _product_url(product_id, utm_source=utm_source, utm_medium=utm_medium, utm_campaign=utm_campaign)
    etag = _etag_for(product_id, product_url, size, level, margin, fg, bg, "png")
    return Response(status_code=204, headers={"ETag": etag, "Cache-Control": "public, max-age=86400"})

