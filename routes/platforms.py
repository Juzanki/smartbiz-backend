from __future__ import annotations
# backend/routes/platforms.py
import hashlib
from enum import Enum
from typing import Optional, List, Any, Dict
from contextlib import suppress
from datetime import datetime, timezone

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Path,
    Header, Response
)
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.db import get_db
from backend.auth import get_current_user

# ---------- Models ----------
try:
    from backend.models.connected_platform import ConnectedPlatform
    from backend.models.user import User
except Exception as e:
    raise RuntimeError("Missing models ConnectedPlatform/User") from e

# ---------- Schemas (fallbacks ikiwa hazipo) ----------
with suppress(Exception):
    from backend.schemas import PlatformConnectRequest, PlatformOut  # preferred

if "PlatformConnectRequest" not in globals() or "PlatformOut" not in globals():
    from pydantic import BaseModel, Field

    class PlatformKind(str, Enum):
        telegram = "telegram"
        whatsapp = "whatsapp"
        instagram = "instagram"
        facebook = "facebook"
        tiktok = "tiktok"
        youtube = "youtube"
        x = "x"  # twitter/x
        pesapal = "pesapal"
        custom = "custom"

    class PlatformConnectRequest(BaseModel):
        platform: PlatformKind = Field(..., description="mf. telegram, whatsapp, etc")
        access_token: str = Field(..., min_length=8)
        meta: Optional[Dict[str, Any]] = None

    class PlatformOut(BaseModel):
        id: int
        user_id: int
        platform: str
        access_token: Optional[str] = None  # masked in responses
        meta: Optional[Dict[str, Any]] = None
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None

        class Config:  # pyd v1
            orm_mode = True
        model_config = {"from_attributes": True}  # pyd v2

# ---------- Optional utils: encryption & verify ----------
# If you have secure helpers, weâ€™ll use them; otherwise safe fallbacks.
with suppress(Exception):
    from backend.utils.secrets import encrypt_token, decrypt_token, mask_token  # type: ignore
if "encrypt_token" not in globals():
    def encrypt_token(s: str) -> str:  # NO-OP fallback
        return s
    def decrypt_token(s: str) -> str:  # NO-OP fallback
        return s
    def mask_token(s: Optional[str]) -> str:
        if not s:
            return ""
        if len(s) <= 8:
            return s[0] + "â€¢â€¢â€¢â€¢" + s[-1]
        return s[:4] + "â€¢â€¢â€¢â€¢" * 3 + s[-4:]

with suppress(Exception):
    from backend.utils.platforms import verify_platform_credentials  # type: ignore
if "verify_platform_credentials" not in globals():
    def verify_platform_credentials(platform: str, token: str, meta: Optional[Dict[str, Any]] = None) -> bool:
        # Placeholder: weka call ya API husika hapa (Telegram, Meta, n.k)
        return True

router = APIRouter(prefix="/platforms", tags=["Integrations"])

# ---------- Helpers ----------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _etag_list(rows: List[Any]) -> str:
    if not rows:
        return 'W/"empty"'
    last = max(getattr(r, "updated_at", None) or getattr(r, "created_at", None) or datetime.min for r in rows)
    ids = ",".join(str(getattr(r, "id", 0)) for r in rows[:100])
    base = f"{ids}|{last.isoformat()}"
    return 'W/"' + hashlib.sha256(base.encode()).hexdigest()[:16] + '"'

def _sanitize_out(row: Any) -> PlatformOut:
    """
    Convert ORM -> Out and mask access_token (never expose raw).
    Supports both pydantic v1/v2.
    """
    # figure out token attribute name (access_token or access_token_enc)
    token_val = None
    if hasattr(row, "access_token_enc") and getattr(row, "access_token_enc"):
        token_val = decrypt_token(getattr(row, "access_token_enc"))
    elif hasattr(row, "access_token") and getattr(row, "access_token"):
        token_val = getattr(row, "access_token")

    masked = mask_token(token_val)
    payload = {
        "id": getattr(row, "id"),
        "user_id": getattr(row, "user_id"),
        "platform": getattr(row, "platform"),
        "access_token": masked,
        "meta": getattr(row, "meta", None),
        "created_at": getattr(row, "created_at", None),
        "updated_at": getattr(row, "updated_at", None),
    }
    if hasattr(PlatformOut, "model_validate"):
        return PlatformOut.model_validate(payload)
    return PlatformOut.parse_obj(payload)

def _store_token(row: Any, token: str) -> None:
    """
    Store encrypted if column exists, else plain (legacy).
    """
    enc = encrypt_token(token)
    if hasattr(row, "access_token_enc"):
        row.access_token_enc = enc
        if hasattr(row, "access_token"):
            row.access_token = None  # avoid double store
    elif hasattr(row, "access_token"):
        row.access_token = enc
    else:
        # allow model without token column
        pass

# ===================== CONNECT / UPSERT =====================
@router.post(
    "/connect",
    response_model=PlatformOut,
    status_code=status.HTTP_201_CREATED,
    summary="Unganisha au sasisha token ya platform (idempotent)"
)
def connect_platform(
    payload: PlatformConnectRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    - Idempotent upsert kwa (user_id, platform) unique.
    - Huhifadhi token kwa encryption ikiwa `access_token_enc` ipo.
    - Hurejesha token **ikiwa imemaskiwa** tu.
    """
    platform = str(payload.platform).lower().strip()

    # Upsert by (user_id, platform)
    rec = (
        db.query(ConnectedPlatform)
        .filter(
            ConnectedPlatform.user_id == current_user.id,
            func.lower(ConnectedPlatform.platform) == platform,
        )
        .first()
    )

    if rec:
        # If incoming token is same as existing â†’ no-op (idempotent)
        prev_token = None
        if hasattr(rec, "access_token_enc") and rec.access_token_enc:
            prev_token = decrypt_token(rec.access_token_enc)
        elif hasattr(rec, "access_token") and rec.access_token:
            prev_token = decrypt_token(rec.access_token)

        if prev_token == payload.access_token and getattr(rec, "meta", None) == (payload.meta or getattr(rec, "meta", None)):
            response.headers["Cache-Control"] = "no-store"
            return _sanitize_out(rec)

        # Update token/meta
        _store_token(rec, payload.access_token)
        if hasattr(rec, "meta"):
            rec.meta = payload.meta or getattr(rec, "meta", None)
        if hasattr(rec, "updated_at"):
            rec.updated_at = _utcnow()
        db.commit()
        db.refresh(rec)
        response.headers["Cache-Control"] = "no-store"
        return _sanitize_out(rec)

    # Create new
    rec = ConnectedPlatform(
        user_id=current_user.id,
        platform=platform,
        meta=getattr(payload, "meta", None),
        created_at=_utcnow() if hasattr(ConnectedPlatform, "created_at") else None,
        updated_at=_utcnow() if hasattr(ConnectedPlatform, "updated_at") else None,
    )
    _store_token(rec, payload.access_token)
    db.add(rec)
    db.commit()
    db.refresh(rec)
    response.headers["Cache-Control"] = "no-store"
    return _sanitize_out(rec)

# ===================== LIST =====================
@router.get(
    "",
    response_model=List[PlatformOut],
    summary="Orodha ya platforms zilizounganishwa (pagination + search + ETag/304)"
)
def list_connected_platforms(
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    q: Optional[str] = Query(None, description="tafuta kwa platform name"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    qry = (
        db.query(ConnectedPlatform)
        .filter(ConnectedPlatform.user_id == current_user.id)
    )
    if q:
        like = f"%{q.lower()}%"
        qry = qry.filter(func.lower(ConnectedPlatform.platform).ilike(like))

    qry = qry.order_by(
        getattr(ConnectedPlatform, "updated_at", getattr(ConnectedPlatform, "id")).desc()
    )
    total = qry.count()
    rows = qry.offset(offset).limit(limit).all()

    etag = _etag_list(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=15"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    return [_sanitize_out(r) for r in rows]

# ===================== GET ONE =====================
@router.get(
    "/{platform}",
    response_model=PlatformOut,
    summary="Pata status ya platform moja"
)
def get_platform(
    platform: str = Path(..., description="mf. telegram"),
    response: Response = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    p = platform.lower().strip()
    rec = (
        db.query(ConnectedPlatform)
        .filter(
            ConnectedPlatform.user_id == current_user.id,
            func.lower(ConnectedPlatform.platform) == p,
        )
        .first()
    )
    if not rec:
        raise HTTPException(status_code=404, detail="Platform not connected")

    if response is not None:
        response.headers["Cache-Control"] = "no-store"
    return _sanitize_out(rec)

# ===================== UPDATE TOKEN =====================
from pydantic import BaseModel, Field
class TokenUpdate(BaseModel):
    access_token: str = Field(..., min_length=8)
    meta: Optional[Dict[str, Any]] = None

@router.put(
    "/{platform}/token",
    response_model=PlatformOut,
    summary="Sasisha token ya platform (optimistic no-op)"
)
def update_platform_token(
    platform: str,
    payload: TokenUpdate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    p = platform.lower().strip()
    rec = (
        db.query(ConnectedPlatform)
        .filter(
            ConnectedPlatform.user_id == current_user.id,
            func.lower(ConnectedPlatform.platform) == p,
        )
        .first()
    )
    if not rec:
        raise HTTPException(status_code=404, detail="Platform not connected")

    # No-op if same
    old = None
    if hasattr(rec, "access_token_enc") and rec.access_token_enc:
        old = decrypt_token(rec.access_token_enc)
    elif hasattr(rec, "access_token") and rec.access_token:
        old = decrypt_token(rec.access_token)
    if old == payload.access_token and getattr(rec, "meta", None) == (payload.meta or getattr(rec, "meta", None)):
        response.headers["Cache-Control"] = "no-store"
        return _sanitize_out(rec)

    _store_token(rec, payload.access_token)
    if hasattr(rec, "meta"):
        rec.meta = payload.meta or getattr(rec, "meta", None)
    if hasattr(rec, "updated_at"):
        rec.updated_at = _utcnow()
    db.commit()
    db.refresh(rec)

    response.headers["Cache-Control"] = "no-store"
    return _sanitize_out(rec)

# ===================== VERIFY CREDENTIALS =====================
@router.post(
    "/{platform}/verify",
    response_model=dict,
    summary="Thibitisha token kwa mtoa huduma husika"
)
def verify_platform(
    platform: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    p = platform.lower().strip()
    rec = (
        db.query(ConnectedPlatform)
        .filter(
            ConnectedPlatform.user_id == current_user.id,
            func.lower(ConnectedPlatform.platform) == p,
        )
        .first()
    )
    if not rec:
        raise HTTPException(status_code=404, detail="Platform not connected")

    token = None
    if hasattr(rec, "access_token_enc") and rec.access_token_enc:
        token = decrypt_token(rec.access_token_enc)
    elif hasattr(rec, "access_token"):
        token = decrypt_token(rec.access_token)

    ok = verify_platform_credentials(p, token or "", getattr(rec, "meta", None))
    return {"platform": p, "verified": bool(ok)}

# ===================== DISCONNECT =====================
@router.delete(
    "/{platform}",
    response_model=dict,
    summary="Ondoa muunganiko wa platform"
)
def disconnect_platform(
    platform: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    p = platform.lower().strip()
    rec = (
        db.query(ConnectedPlatform)
        .filter(
            ConnectedPlatform.user_id == current_user.id,
            func.lower(ConnectedPlatform.platform) == p,
        )
        .first()
    )
    if not rec:
        raise HTTPException(status_code=404, detail="Platform not connected")

    db.delete(rec)
    db.commit()
    return {"detail": f"Disconnected {p}"}

