from __future__ import annotations
# backend/routes/product_qr.py
import os
import io
import base64
import hashlib
from pathlib import Path
from typing import Optional, Tuple
from contextlib import suppress
from urllib.parse import urljoin, urlencode, urlparse, parse_qsl, urlunparse

from fastapi import (
    APIRouter, Depends, HTTPException, Response, Query, Header, status
)
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models.product import Product
with suppress(Exception):
    from backend.auth import get_current_user

# Jitahidi kutumia util yako kama ipo; vinginevyo tutatumia fallback ya qrcode
qr_util = None
with suppress(Exception):
    from backend.utils.qr_utils import generate_qr_code as qr_util  # type: ignore

# Fallback ya kutengeneza PNG bytes moja kwa moja
with suppress(Exception):
    import qrcode
    from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q, ERROR_CORRECT_H

router = APIRouter(prefix="/qr", tags=["QR Codes"])

# ----------------------- Helpers -----------------------

def _public_base_url() -> str:
    # Jaribu hizi ENV; rudisha fallback salama
    for k in ("RAILWAY_PUBLIC_URL", "NETLIFY_PUBLIC_URL", "VITE_API_URL"):
        v = os.getenv(k)
        if v and v.startswith("http"):
            return v.rstrip("/")
    return "https://yourdomain.com"

def _build_product_url(product_id: int, *, order_path: str, utm: dict) -> str:
    base = _public_base_url()
    url = urljoin(base + "/", f"{order_path.strip('/')}/{product_id}")
    if utm:
        u = urlparse(url)
        q = dict(parse_qsl(u.query))
        q.update({k: v for k, v in utm.items() if v})
        url = urlunparse(u._replace(query=urlencode(q)))
    return url

def _validate_hex(color: str) -> str:
    c = color.strip()
    if c.startswith("#"): c = c[1:]
    if len(c) not in (3, 6) or any(ch.lower() not in "0123456789abcdef" for ch in c):
        raise HTTPException(status_code=400, detail=f"Invalid color: #{c}")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return f"#{c.lower()}"

def _etag_for(seed: str) -> str:
    return 'W/"' + hashlib.sha256(seed.encode()).hexdigest()[:16] + '"'

def _qr_png_fallback(data: str, *, size: int, level: str, margin: int, fg: str, bg: str) -> bytes:
    if "qrcode" not in globals():
        raise HTTPException(status_code=500, detail="qrcode library missing and qr_utils not available")
    ec_map = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "Q": ERROR_CORRECT_Q, "H": ERROR_CORRECT_H}
    ec = ec_map.get(level.upper(), ERROR_CORRECT_M)
    box = max(2, min(40, size // 42))  # approx
    qr = qrcode.QRCode(version=None, error_correction=ec, box_size=box, border=max(0, min(16, margin)))
    qr.add_data(data); qr.make(fit=True)
    img = qr.make_image(fill_color=fg, back_color=bg)
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return buf.getvalue()

def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def _storage_paths(filename: str) -> Tuple[Path, str]:
    """
    Rudisha (local_path, public_url_or_path).
    Tumia env:
      - QR_MEDIA_DIR (default: media/qr)
      - QR_PUBLIC_BASE (kama unaserve static kwa URL; vinginevyo turudishe local path)
    """
    media_dir = Path(os.getenv("QR_MEDIA_DIR", "media/qr")).resolve()
    _ensure_dir(media_dir)
    local_path = media_dir / filename
    public_base = os.getenv("QR_PUBLIC_BASE")  # ex: https://cdn.yourdomain.com/qr/
    if public_base and public_base.startswith("http"):
        return local_path, urljoin(public_base.rstrip("/") + "/", filename)
    return local_path, str(local_path)

# ----------------------- Core generator -----------------------

def _generate_png_bytes(data: str, *, size: int, level: str, margin: int, fg: str, bg: str) -> bytes:
    # Jaribu util yako kwanza ikiwa ina-reply bytes/base64 kulingana na signature
    if qr_util:
        with suppress(Exception):
            # Ikiwa util inarudisha path (tunataka bytes hapa) â€” tutashusha fallback
            out = qr_util(data, filename=None)  # type: ignore
            # wengi hurudisha path string; acha iende fallback chini
        with suppress(Exception):
            # Labda util yako inarudisha base64 moja kwa moja
            b64 = qr_util(data)  # type: ignore
            if isinstance(b64, str) and len(b64) > 100:
                return base64.b64decode(b64.encode("ascii"))
    # fallback ya qrcode
    return _qr_png_fallback(data, size=size, level=level, margin=margin, fg=fg, bg=bg)

def _store_png_file(png: bytes, filename: str) -> Tuple[Path, str]:
    local_path, public = _storage_paths(filename)
    local_path.write_bytes(png)
    return local_path, public

# ----------------------- Endpoints -----------------------

@router.post(
    "/products/{product_id}",
    summary="ðŸ”² Generate QR for a product (PNG or JSON) + hiari kuhifadhi na ku-update DB"
)
def create_product_qr(
    product_id: int,
    response: Response,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) if "get_current_user" in globals() else None,

    # Output & customization
    fmt: str = Query("png", pattern="^(png|json)$", description="png=raw image, json=base64"),
    size: int = Query(512, ge=96, le=2048),
    level: str = Query("M", pattern="^(L|M|Q|H)$"),
    margin: int = Query(4, ge=0, le=16),
    fg: str = Query("#000000"),
    bg: str = Query("#FFFFFF"),

    # UTM & path
    utm_source: Optional[str] = Query(None),
    utm_medium: Optional[str] = Query(None),
    utm_campaign: Optional[str] = Query(None),
    order_path: str = Query("order", description="Relative path e.g. 'order' => /order/<id>"),

    # Storage & idempotency
    store: str = Query("none", pattern="^(none|disk)$", description="Hifadhi PNG kwenye disk au usihifadhi"),
    overwrite: bool = Query(False, description="True kuandika faili upya hata kama lipo"),

    # Caching
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    if product_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid product_id")

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # (hiari) ulinzi wa umiliki
    if current_user is not None and hasattr(Product, "owner_id"):
        owner_id = getattr(product, "owner_id", None)
        if owner_id and owner_id != getattr(current_user, "id", None) and getattr(current_user, "role", "") not in ("admin", "owner"):
            raise HTTPException(status_code=403, detail="Not allowed on this product")

    # sanitize rangi
    fg = _validate_hex(fg); bg = _validate_hex(bg)

    # Tengeneza URL ya order page
    url = _build_product_url(product_id, order_path=order_path, utm={"utm_source": utm_source, "utm_medium": utm_medium, "utm_campaign": utm_campaign})

    # ETag kwa caching ya mobile
    etag = _etag_for(f"{product_id}|{url}|{size}|{level}|{margin}|{fg}|{bg}|{fmt}|{store}")
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=86400"

    # Tengeneza PNG bytes
    png = _generate_png_bytes(url, size=size, level=level, margin=margin, fg=fg, bg=bg)

    stored_url: Optional[str] = None
    stored_path: Optional[str] = None
    # Hifadhi kwenye disk (hiari) na weka DB field
    if store == "disk":
        filename = f"product_{product_id}.png"
        local_path, public = _storage_paths(filename)
        if overwrite or not local_path.exists():
            local_path.write_bytes(png)
        stored_path, stored_url = str(local_path), public
        # weka DB kama ina field
        with suppress(Exception):
            if hasattr(product, "qr_code_url"):
                product.qr_code_url = stored_url
                db.commit()

    if fmt == "png":
        return StreamingResponse(io.BytesIO(png), media_type="image/png")

    # json (base64)
    b64 = base64.b64encode(png).decode("ascii")
    return JSONResponse({
        "product_id": product_id,
        "link": url,
        "qr_code_base64": b64,
        "stored_url": stored_url,
        "stored_path": stored_path,
        "size": size, "level": level, "margin": margin, "fg": fg, "bg": bg
    })

@router.get(
    "/generate",
    summary="ðŸ”³ Generate QR (data yoyote) â€” PNG au JSON (base64)"
)
def generate_qr(
    response: Response,
    data: str = Query(..., description="URL/data ya QR"),
    fmt: str = Query("png", pattern="^(png|json)$"),
    size: int = Query(512, ge=96, le=2048),
    level: str = Query("M", pattern="^(L|M|Q|H)$"),
    margin: int = Query(4, ge=0, le=16),
    fg: str = Query("#000000"),
    bg: str = Query("#FFFFFF"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    if not data or len(data.strip()) < 1:
        raise HTTPException(status_code=400, detail="`data` is required")
    fg = _validate_hex(fg); bg = _validate_hex(bg)

    etag = _etag_for(f"{data}|{size}|{level}|{margin}|{fg}|{bg}|{fmt}")
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=86400"

    png = _generate_png_bytes(data, size=size, level=level, margin=margin, fg=fg, bg=bg)
    if fmt == "png":
        return StreamingResponse(io.BytesIO(png), media_type="image/png")
    b64 = base64.b64encode(png).decode("ascii")
    return {"qr_code_base64": b64, "size": size, "level": level, "margin": margin, "fg": fg, "bg": bg}

