from __future__ import annotations
# backend/routes/referral.py
import os
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Any, Dict
from contextlib import suppress
from urllib.parse import urljoin

from fastapi import (
    APIRouter, Depends, HTTPException, Query, Request, Response, Header
)
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models.user import User
from backend.models.referral_log import ReferralLog
# (hiari) ukipata ReferralClick model kwa click-tracking:
with suppress(Exception):
from backend.models.referral_log import ReferralLog
router = APIRouter(prefix="/ref", tags=["Referral Tracker"])

COOKIE_NAME = "sb_ref"
COOKIE_MAX_AGE = 30 * 24 * 60 * 60  # 30 days
COOKIE_SAMESITE = "Lax"
COOKIE_SECURE = (os.getenv("ENVIRONMENT", "development").lower() == "production")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me")  # lazima uwe nayo kwenye .env prod

# signed cookie (itsdangerous)
_serializer = None
with suppress(Exception):
    from itsdangerous import URLSafeSerializer
    _serializer = URLSafeSerializer(SECRET_KEY, salt="referral-cookie")

def _utc() -> datetime:
    return datetime.now(timezone.utc)

def _public_base_url() -> str:
    for k in ("RAILWAY_PUBLIC_URL", "NETLIFY_PUBLIC_URL", "VITE_API_URL"):
        v = os.getenv(k)
        if v and v.startswith("http"):
            return v.rstrip("/")
    return "https://smartbiz.live"

def _landing_path_default() -> str:
    # ukurasa wa kutua baada ya referral
    return "/"

def _sign(data: Dict[str, Any]) -> str:
    if _serializer:
        return _serializer.dumps(data)
    # fallback: unsigned (si salama, tumia kwa dev tu)
    import json
    return json.dumps(data, separators=(",", ":"))

def _unsign(value: str) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    if _serializer:
        with suppress(Exception):
            data = _serializer.loads(value)
            if isinstance(data, dict):
                return data
        return None
    # fallback: unsigned
    with suppress(Exception):
        import json
        return json.loads(value)
    return None

def _set_cookie(response: Response, data: Dict[str, Any]) -> None:
    signed = _sign(data)
    response.set_cookie(
        COOKIE_NAME, signed,
        max_age=COOKIE_MAX_AGE, httponly=True,
        secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE, path="/"
    )

def _clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")

def _ref_cookie(request: Request) -> Optional[Dict[str, Any]]:
    raw = request.cookies.get(COOKIE_NAME)
    return _unsign(raw) if raw else None

def _etag_for(data: Dict[str, Any]) -> str:
    seed = repr(sorted(data.items())) if data else "none"
    return 'W/"' + hashlib.sha256(seed.encode()).hexdigest()[:16] + '"'

# ============================ ROUTES ============================

@router.get(
    "/{username}",
    summary="Track referral na kuweka cookie (redirect to landing/next)"
)
def track_referral(
    username: str,
    request: Request,
    db: Session = Depends(get_db),
    next_url: Optional[str] = Query(None, description="relative path to redirect e.g. /signup"),
    campaign: Optional[str] = Query(None, description="campaign/utm"),
    source: Optional[str] = Query(None, description="utm_source"),
    medium: Optional[str] = Query(None, description="utm_medium"),
):
    # 1) Thibitisha referrer
    ref_user = db.query(User).filter(User.username == username).first()
    if not ref_user:
        # bad ref => peleka kwa landing bila cookie
        target = urljoin(_public_base_url() + "/", next_url or _landing_path_default().lstrip("/"))
        return RedirectResponse(url=target, status_code=307)

    # 2) Andaa response ya redirect
    target = urljoin(_public_base_url() + "/", (next_url or _landing_path_default()).lstrip("/"))
    resp = RedirectResponse(url=target, status_code=307)

    # 3) Weka cookie (signed)
    ref_payload = {
        "u": username,              # referrer username
        "uid": ref_user.id,         # referrer id
        "c": campaign,              # campaign/utm_campaign
        "src": source,              # utm_source
        "md": medium,               # utm_medium
        "ts": int(_utc().timestamp()),
        "v": 1                      # version for future migrations
    }
    _set_cookie(resp, ref_payload)

    # 4) Hifadhi click (background-light)
    with suppress(Exception):
        if "ReferralClick" in globals():
            rc = ReferralClick(
                referrer_id=ref_user.id,
                campaign=campaign,
                source=source,
                medium=medium,
                ip_address=str(request.client.host) if request.client else None,
                user_agent=request.headers.get("user-agent"),
                created_at=_utc(),
            )
            db.add(rc); db.commit()

    return resp


@router.get(
    "/check",
    summary="Angalia referral cookie ya sasa (kwa UI)",
)
def check_referral(
    request: Request,
    response: Response,
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    data = _ref_cookie(request) or {}
    etag = _etag_for(data)
    if if_none_match and if_none_match == etag:
        return Response(status_code=304)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=30"
    return data


@router.post(
    "/clear",
    summary="Ondoa referral cookie (logout/referrer change)"
)
def clear_referral(response: Response):
    _clear_cookie(response)
    return {"detail": "cleared"}


# ============================ UTIL: Tumia wakati wa CHECKOUT ============================
def consume_referral_on_checkout(
    *,
    db: Session,
    buyer_id: int,
    order_id: int,
    order_amount: float,
    request: Optional[Request] = None,
    commission_rate: float = 0.05,  # 5% default
) -> Optional[ReferralLog]:
    """
    Tumia hii ndani ya route yako ya 'create order / charge successful'.
    - Inaweka rekodi 1 idempotent kwa (buyer_id, order_id)
    - Inazuia self-referral (buyer == referrer)
    - Inaheshimu cookie iliyowekwa na /ref/{username}
    - Ikitokomea cookie, unaweza pia kuleta referrer kwa querystring au metadata ya order

    Inarejesha ReferralLog au None ikiwa hakuna referral.
    """
    # 0) Angalia kama tayari tume-log huu order_id (idempotent)
    existing = (
        db.query(ReferralLog)
        .filter(ReferralLog.order_id == order_id)
        .first()
    )
    if existing:
        return existing

    # 1) Pata data ya referral
    ref_data: Dict[str, Any] = {}
    if request:
        ref_data = _ref_cookie(request) or {}

    username = ref_data.get("u")
    referrer_id = ref_data.get("uid")
    campaign = ref_data.get("c")
    source = ref_data.get("src")
    medium = ref_data.get("md")

    if not referrer_id and username:
        # fallback: resolve user id
        with suppress(Exception):
            u = db.query(User).filter(User.username == username).first()
            if u:
                referrer_id = u.id

    if not referrer_id:
        return None  # no referral present

    if referrer_id == buyer_id:
        return None  # self-referral is ignored

    # 2) Hesabu commission (kulingana na rate)
    commission_amount = round(float(order_amount) * float(commission_rate), 2)

    # 3) Unda log
    row = ReferralLog(
        referrer_id=referrer_id,
        buyer_id=buyer_id,
        order_id=order_id,
        amount=order_amount,
        commission_amount=commission_amount,
        campaign=campaign,
        source=source,
        medium=medium,
        created_at=_utc(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # 4) (hiari) unaweza kufuta cookie ili kuepuka re-use kwenye order inayofuata
    # resp: Response ; _clear_cookie(resp)

    return row




