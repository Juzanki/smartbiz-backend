# backend/routes/sellers_store_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
SmartBiz • Sellers + Store API (Ultra-Stable, Production) + ✅ EarnHub Missions (Persistent)

Key goal (your issue):
- EarnHub CLAIM should NOT disappear on refresh.
✅ Fix: everything is persisted in DB: wallet + daily + streak + ledger
✅ Fix: ad claim has cooldown (server-side), streak is once-per-day (server-side)
✅ Fix: streak "claimed" resets automatically each new UTC day
✅ Adds: GET /earnhub/history for History tab (real ledger)

EXISTING (kept):
- GET  /me/seller
- GET  /sellers/me
- GET  /stores/me
- GET  /stores/me/profile
- GET  /stores/me/metrics
- POST /sellers/start
- POST /seller/onboard

EARNHUB:
- GET  /earnhub/overview
- POST /earnhub/ad/claim
- POST /earnhub/streak/claim
- GET  /earnhub/history          ✅ NEW
- Referral/Share/Proof/Survey/Quests endpoints kept
- Compat: GET /earn/stats + GET /earnings/overview
"""

from contextlib import suppress
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Any, Dict, List, Literal
from uuid import UUID, uuid4
import hashlib
import os
import re
import json

from fastapi import APIRouter, Depends, HTTPException, Response, status, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import text


# ─────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────
try:
    from backend.db import get_db  # type: ignore
except Exception:  # pragma: no cover
    from db import get_db  # type: ignore


# ─────────────────────────────────────────────────────────────
# Auth import (primary)
# ─────────────────────────────────────────────────────────────
try:
    from backend.auth import get_current_user
except Exception:  # pragma: no cover
    def get_current_user():  # type: ignore
        class _U:
            id: Optional[str] = None
            email: Optional[str] = None
            username: Optional[str] = None
            is_seller: bool = False
            seller_id: Optional[str] = None
        return _U()


JWT_SECRET = os.getenv("JWT_SECRET") or os.getenv("SECRET_KEY") or ""
JWT_ALG = os.getenv("JWT_ALG") or "HS256"

EARNHUB_AUTOCREATE = str(os.getenv("EARNHUB_AUTOCREATE", "")).strip().lower() in ("1", "true", "yes", "on")

# EarnHub config (safe defaults)
EARNHUB_DAILY_CAP = int(os.getenv("EARNHUB_DAILY_CAP", "600"))
EARNHUB_AD_REWARD = int(os.getenv("EARNHUB_AD_REWARD", "25"))
EARNHUB_STREAK_BASE = int(os.getenv("EARNHUB_STREAK_BASE", "10"))
EARNHUB_STREAK_STEP = int(os.getenv("EARNHUB_STREAK_STEP", "5"))
EARNHUB_AD_COOLDOWN_SECONDS = int(os.getenv("EARNHUB_AD_COOLDOWN", "35"))  # seconds


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_utc() -> date:
    return _now().date()


def _today_utc_date_str() -> str:
    return _today_utc().isoformat()


def _etag(payload_key: str) -> str:
    return f'W/"{hashlib.sha256(payload_key.encode("utf-8")).hexdigest()[:16]}"'


def _jwt_decode(token: str) -> Dict[str, Any]:
    token = (token or "").strip()
    if not token:
        return {}
    if token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1].strip()

    # python-jose
    with suppress(Exception):
        from jose import jwt  # type: ignore
        if JWT_SECRET:
            return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        # no secret => unverified claims (still useful for uid)
        return jwt.get_unverified_claims(token)

    # PyJWT
    with suppress(Exception):
        import jwt as pyjwt  # type: ignore
        if JWT_SECRET:
            return pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        return pyjwt.decode(token, options={"verify_signature": False})

    return {}


def _is_uuid(s: str) -> bool:
    try:
        UUID(str(s))
        return True
    except Exception:
        return False


def _resolve_uid(user: Any, authorization: Optional[str], x_access_token: Optional[str]) -> Optional[str]:
    # 1) from dependency user.id
    uid = str(getattr(user, "id", "") or "").strip()
    if uid and _is_uuid(uid):
        return uid

    # 2) from decoded token (support many claim keys)
    token = (authorization or x_access_token or "").strip()
    payload = _jwt_decode(token)
    uid2 = payload.get("sub") or payload.get("user_id") or payload.get("id") or payload.get("uid") or ""
    uid2 = str(uid2 or "").strip()
    return uid2 if (uid2 and _is_uuid(uid2)) else None


def _row_or_default(db: Session, sql: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    with suppress(Exception):
        r = db.execute(text(sql), params).mappings().first()
        if r:
            return dict(r)
    return None


def _rows(db: Session, sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    with suppress(Exception):
        rr = db.execute(text(sql), params).mappings().all()
        return [dict(x) for x in rr] if rr else []
    return []


def _scalar_int(db: Session, sql: str, params: Dict[str, Any]) -> int:
    with suppress(Exception):
        v = db.execute(text(sql), params).scalar()
        if v is not None:
            return int(v)
    return 0


def _scalar_str(db: Session, sql: str, params: Dict[str, Any]) -> str:
    with suppress(Exception):
        v = db.execute(text(sql), params).scalar()
        if v is not None:
            return str(v)
    return ""


def _safe_slug(s: str) -> str:
    s = str(s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:60] if s else ""


def _cap_left(daily_cap: int, earned_today: int) -> int:
    return max(0, int(daily_cap) - int(earned_today))


def _streak_reward(day_num: int) -> int:
    d = max(1, min(7, int(day_num or 1)))
    return int(EARNHUB_STREAK_BASE + (d - 1) * EARNHUB_STREAK_STEP)


# ─────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────
router = APIRouter(tags=["Sellers", "Store", "EarnHub"])


# ─────────────────────────────────────────────────────────────
# Schemas (Sellers/Store)
# ─────────────────────────────────────────────────────────────
class SellerOut(BaseModel):
    is_seller: bool
    seller_id: Optional[str] = None
    status: str = "inactive"  # inactive|pending|active|suspended
    steps_required: List[str] = []
    updated_at: datetime = Field(default_factory=_now)


class StoreOut(BaseModel):
    id: Optional[str] = None
    owner_id: Optional[str] = None
    name: str = "My Store"
    slug: Optional[str] = None
    logo_url: Optional[str] = None
    cover_url: Optional[str] = None
    bio: Optional[str] = None
    followers_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class MetricsOut(BaseModel):
    products: int = 0
    products_published: int = 0
    products_private: int = 0
    orders: int = 0
    earnings_cents: int = 0
    views_30d: int = 0
    updated_at: datetime = Field(default_factory=_now)


# ─────────────────────────────────────────────────────────────
# Schemas (EarnHub)
# ─────────────────────────────────────────────────────────────
EarnHubMissionType = Literal["invite", "survey", "quests", "sponsored", "share_stream", "ad", "streak"]


class EarnHubOverviewOut(BaseModel):
    balance: int = 0
    daily_cap: int = EARNHUB_DAILY_CAP
    earned_today: int = 0
    streak: Dict[str, Any] = Field(default_factory=lambda: {"active": True, "day": 1, "claimed": False})
    referral_code: str = "SMARTBIZ-NEW"
    last_claim_at: Optional[str] = None
    updated_at: str = Field(default_factory=lambda: _now().isoformat())


class EarnHubActionOut(BaseModel):
    ok: bool = True
    earned: int = 0
    balance: int = 0
    earned_today: int = 0
    daily_cap: int = EARNHUB_DAILY_CAP
    cooldown: int = 0
    streak: Optional[Dict[str, Any]] = None
    referral_code: Optional[str] = None
    message: str = "ok"


class EarnHubLedgerItem(BaseModel):
    id: str
    kind: str
    amount: int
    at_ms: int
    meta: Optional[Dict[str, Any]] = None


class EarnHubHistoryOut(BaseModel):
    items: List[EarnHubLedgerItem] = []
    updated_at: str = Field(default_factory=lambda: _now().isoformat())


class ReferralMeOut(BaseModel):
    code: str
    url: str
    total_invites: int = 0
    active_referrals: int = 0
    earned_sc: int = 0
    updated_at: datetime = Field(default_factory=_now)


class ReferralRegenerateOut(BaseModel):
    code: str
    url: str
    updated_at: datetime = Field(default_factory=_now)


class ShareClaimIn(BaseModel):
    share_type: Literal["stream", "product"] = "stream"
    platform: str = Field(default="WhatsApp", max_length=40)
    url: str = Field(..., max_length=2048)
    title: Optional[str] = Field(default=None, max_length=140)
    message: Optional[str] = Field(default=None, max_length=1000)


class ShareClaimOut(BaseModel):
    id: str
    status: Literal["shared", "submitted", "pending"] = "shared"
    earned_sc: int = 0
    created_at: datetime = Field(default_factory=_now)


class ProofIn(BaseModel):
    mission: EarnHubMissionType
    reference_id: Optional[str] = Field(default=None, max_length=80)
    proof_url: str = Field(..., max_length=2048)
    note: Optional[str] = Field(default=None, max_length=500)


class ProofOut(BaseModel):
    id: str
    mission: EarnHubMissionType
    reference_id: Optional[str] = None
    status: Literal["received", "verified", "rejected"] = "received"
    earned_sc: int = 0
    created_at: datetime = Field(default_factory=_now)


class SponsoredOfferOut(BaseModel):
    id: str
    title: str
    description: str
    reward_sc: int
    action: str
    url: Optional[str] = None


class QuestsOut(BaseModel):
    id: str
    title: str
    description: str
    reward_sc: int
    status: Literal["open", "claimed"] = "open"


class GenericOk(BaseModel):
    ok: bool = True
    message: str = "ok"
    updated_at: datetime = Field(default_factory=_now)


# ─────────────────────────────────────────────────────────────
# SELLER helpers (unchanged)
# ─────────────────────────────────────────────────────────────
def _fetch_user_row(db: Session, uid: str) -> Dict[str, Any]:
    row = _row_or_default(
        db,
        """
        SELECT
          id::text AS id,
          COALESCE(is_seller, FALSE) AS is_seller,
          CASE WHEN seller_id IS NULL THEN NULL ELSE seller_id::text END AS seller_id,
          COALESCE(seller_status, NULL) AS seller_status,
          COALESCE(username, NULL) AS username,
          COALESCE(email, NULL) AS email
        FROM users
        WHERE id = :uid
        LIMIT 1
        """,
        {"uid": uid},
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return row


def _ensure_seller(db: Session, uid: str) -> SellerOut:
    u = _fetch_user_row(db, uid)
    if bool(u.get("is_seller")):
        return SellerOut(
            is_seller=True,
            seller_id=u.get("seller_id"),
            status=u.get("seller_status") or "active",
            updated_at=_now(),
        )

    sid = str(uuid4())
    db.execute(
        text(
            """
            UPDATE users SET
              is_seller = TRUE,
              seller_id = :sid,
              seller_status = COALESCE(seller_status, 'active'),
              updated_at = NOW()
            WHERE id = :uid
            """
        ),
        {"sid": sid, "uid": uid},
    )
    db.commit()
    return SellerOut(is_seller=True, seller_id=sid, status="active", updated_at=_now())


# ─────────────────────────────────────────────────────────────
# EarnHub: optional table bootstrap (kept)
# ─────────────────────────────────────────────────────────────
def _ensure_earnhub_tables(db: Session) -> None:
    if not EARNHUB_AUTOCREATE:
        return

    stmts = [
        """
        CREATE TABLE IF NOT EXISTS earnhub_referrals (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id UUID NOT NULL,
          code TEXT NOT NULL UNIQUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_earnhub_referrals_user_id ON earnhub_referrals(user_id);",
        """
        CREATE TABLE IF NOT EXISTS earnhub_share_attempts (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id UUID NOT NULL,
          share_type TEXT NOT NULL,
          platform TEXT NOT NULL,
          url TEXT NOT NULL,
          title TEXT NULL,
          message TEXT NULL,
          status TEXT NOT NULL DEFAULT 'shared',
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_earnhub_share_user_id ON earnhub_share_attempts(user_id, created_at DESC);",
        """
        CREATE TABLE IF NOT EXISTS earnhub_proofs (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id UUID NOT NULL,
          mission TEXT NOT NULL,
          reference_id TEXT NULL,
          proof_url TEXT NOT NULL,
          note TEXT NULL,
          status TEXT NOT NULL DEFAULT 'received',
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_earnhub_proofs_user_id ON earnhub_proofs(user_id, created_at DESC);",
        """
        CREATE TABLE IF NOT EXISTS earnhub_events (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id UUID NOT NULL,
          event_type TEXT NOT NULL,
          ref_id TEXT NULL,
          payload JSONB NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_earnhub_events_user_id ON earnhub_events(user_id, created_at DESC);",
    ]

    with suppress(Exception):
        for s in stmts:
            db.execute(text(s))
        db.commit()


def _app_origin() -> str:
    o = (os.getenv("APP_PUBLIC_ORIGIN") or os.getenv("PUBLIC_ORIGIN") or "").strip()
    return o.rstrip("/") if o else "https://smartbiz.site"


def _make_ref_code(user_row: Dict[str, Any]) -> str:
    name = (user_row.get("username") or user_row.get("email") or "smartbiz").split("@")[0]
    base = _safe_slug(name).upper().replace("-", "")[:10] or "SMARTBIZ"
    rand = hashlib.sha256(f"{user_row.get('id')}|{uuid4()}".encode("utf-8")).hexdigest()[:6].upper()
    return f"SBZ-{base}-{rand}"


def _get_or_create_referral(db: Session, uid: str) -> str:
    _ensure_earnhub_tables(db)

    code = _scalar_str(db, "SELECT code FROM earnhub_referrals WHERE user_id=:uid LIMIT 1", {"uid": uid})
    if code:
        return code

    u = _fetch_user_row(db, uid)
    code = _make_ref_code(u)

    if not EARNHUB_AUTOCREATE:
        return "SMARTBIZ-NEW"

    for _ in range(4):
        try:
            db.execute(text("INSERT INTO earnhub_referrals(user_id, code) VALUES (:uid, :code)"), {"uid": uid, "code": code})
            db.commit()
            return code
        except Exception:
            with suppress(Exception):
                db.rollback()
            code = _make_ref_code(u)

    return code


def _normalize_http_url(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        return ""
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if "." in u:
        return f"https://{u}"
    return u


# ─────────────────────────────────────────────────────────────
# ✅ EarnHub PRO helpers (persistent + safe)
# ─────────────────────────────────────────────────────────────
def _ensure_wallet_row(db: Session, uid: str) -> None:
    db.execute(
        text("INSERT INTO earnhub_wallets(user_id, balance) VALUES(:uid, 0) ON CONFLICT (user_id) DO NOTHING"),
        {"uid": uid},
    )


def _get_wallet_balance(db: Session, uid: str) -> int:
    _ensure_wallet_row(db, uid)
    r = _row_or_default(db, "SELECT balance::bigint AS balance FROM earnhub_wallets WHERE user_id=:uid", {"uid": uid})
    return int(r["balance"]) if r and r.get("balance") is not None else 0


def _wallet_add(db: Session, uid: str, amount: int) -> int:
    amt = int(amount)
    db.execute(
        text(
            """
            INSERT INTO earnhub_wallets(user_id, balance)
            VALUES(:uid, :amt)
            ON CONFLICT (user_id)
            DO UPDATE SET balance = earnhub_wallets.balance + EXCLUDED.balance,
                          updated_at = NOW()
            """
        ),
        {"uid": uid, "amt": amt},
    )
    r = _row_or_default(db, "SELECT balance::bigint AS balance FROM earnhub_wallets WHERE user_id=:uid", {"uid": uid})
    return int(r["balance"]) if r and r.get("balance") is not None else 0


def _ledger_add(db: Session, uid: str, kind: str, amount: int, meta: Optional[Dict[str, Any]] = None) -> None:
    db.execute(
        text("INSERT INTO earnhub_ledger(user_id, kind, amount, meta) VALUES(:uid, :kind, :amt, :meta::jsonb)"),
        {"uid": uid, "kind": str(kind), "amt": int(amount), "meta": json.dumps(meta or {})},
    )


def _get_daily(db: Session, uid: str) -> Dict[str, Any]:
    day = _today_utc()
    r = _row_or_default(
        db,
        """
        SELECT earned_today::bigint AS earned_today,
               last_ad_claim_at
        FROM earnhub_daily
        WHERE user_id=:uid AND day=:day
        """,
        {"uid": uid, "day": day},
    )
    earned_today = int(r["earned_today"]) if r and r.get("earned_today") is not None else 0
    last_ad_claim_at = r.get("last_ad_claim_at") if r else None
    return {"day": day, "earned_today": earned_today, "daily_cap": EARNHUB_DAILY_CAP, "last_ad_claim_at": last_ad_claim_at}


def _upsert_daily_add(db: Session, uid: str, add_amount: int) -> Dict[str, Any]:
    day = _today_utc()
    db.execute(
        text(
            """
            INSERT INTO earnhub_daily(user_id, day, earned_today)
            VALUES(:uid, :day, :amt)
            ON CONFLICT (user_id, day)
            DO UPDATE SET earned_today = earnhub_daily.earned_today + EXCLUDED.earned_today
            """
        ),
        {"uid": uid, "day": day, "amt": int(add_amount)},
    )
    return _get_daily(db, uid)


def _set_last_ad_claim_at(db: Session, uid: str) -> None:
    day = _today_utc()
    db.execute(
        text(
            """
            INSERT INTO earnhub_daily(user_id, day, earned_today, last_ad_claim_at)
            VALUES(:uid, :day, 0, NOW())
            ON CONFLICT (user_id, day)
            DO UPDATE SET last_ad_claim_at = NOW()
            """
        ),
        {"uid": uid, "day": day},
    )


def _ensure_streak_row(db: Session, uid: str) -> None:
    db.execute(
        text("INSERT INTO earnhub_streaks(user_id, day) VALUES(:uid, 1) ON CONFLICT (user_id) DO NOTHING"),
        {"uid": uid},
    )


def _get_streak(db: Session, uid: str) -> Dict[str, Any]:
    _ensure_streak_row(db, uid)
    r = _row_or_default(
        db,
        """
        SELECT day::int AS day,
               last_claim_date,
               last_claim_at
        FROM earnhub_streaks
        WHERE user_id=:uid
        """,
        {"uid": uid},
    )
    if not r:
        return {"active": True, "day": 1, "claimed": False, "last_claim_at": None}

    last_claim_date = r.get("last_claim_date")
    claimed_today = bool(last_claim_date == _today_utc())
    return {
        "active": True,
        "day": int(r.get("day") or 1),
        "claimed": claimed_today,
        "last_claim_at": r.get("last_claim_at"),
    }


def _ad_cooldown_left_seconds(daily_row: Dict[str, Any]) -> int:
    last = daily_row.get("last_ad_claim_at")
    if not last:
        return 0
    try:
        delta = (_now() - last).total_seconds()
        left = int(EARNHUB_AD_COOLDOWN_SECONDS - delta)
        return max(0, left)
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────
# ENDPOINTS (Sellers/Store) — unchanged behavior
# ─────────────────────────────────────────────────────────────
@router.get("/sellers/me", response_model=SellerOut)
@router.get("/me/seller", response_model=SellerOut)
@router.get("/stores/me", response_model=SellerOut)
def seller_me(
    response: Response,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    if not uid:
        response.headers["Cache-Control"] = "no-store"
        return SellerOut(is_seller=False, seller_id=None, status="inactive", steps_required=["sign_in"], updated_at=_now())

    u = _fetch_user_row(db, uid)
    is_seller = bool(u.get("is_seller"))
    status_val = u.get("seller_status") or ("active" if is_seller else "inactive")

    steps: List[str] = []
    if not is_seller:
        steps = ["enable_seller_mode", "create_store_profile", "add_first_product"]

    response.headers["ETag"] = _etag(f"{uid}|{is_seller}|{status_val}|{','.join(steps)}")
    response.headers["Cache-Control"] = "private, max-age=20"

    return SellerOut(is_seller=is_seller, seller_id=u.get("seller_id"), status=status_val, steps_required=steps, updated_at=_now())


@router.get("/stores/me/profile", response_model=StoreOut)
def store_profile(
    response: Response,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    base = StoreOut(owner_id=uid or None, name=(getattr(user, "username", None) or "My Store"))

    if not uid:
        response.headers["Cache-Control"] = "no-store"
        return base

    row = _row_or_default(
        db,
        """
        SELECT id::text AS id, owner_id::text AS owner_id, name, slug,
               logo_url, cover_url, bio,
               COALESCE(followers_count,0)::int AS followers_count,
               created_at, updated_at
        FROM stores
        WHERE owner_id = :uid
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"uid": uid},
    )

    if not row:
        response.headers["ETag"] = _etag(f"{uid}|nostore")
        response.headers["Cache-Control"] = "private, max-age=20"
        return base

    out = StoreOut(**row)
    response.headers["ETag"] = _etag(f"{uid}|{out.id}|{out.updated_at}")
    response.headers["Cache-Control"] = "private, max-age=20"
    return out


@router.get("/stores/me/metrics", response_model=MetricsOut)
def store_metrics(
    response: Response,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    m = MetricsOut(updated_at=_now())
    if not uid:
        response.headers["Cache-Control"] = "no-store"
        return m

    m.products = _scalar_int(db, "SELECT COUNT(*) FROM products WHERE owner_id=:uid", {"uid": uid})
    m.products_published = _scalar_int(
        db,
        """
        SELECT COUNT(*)
        FROM products
        WHERE owner_id=:uid
          AND status='published'
          AND visibility='public'
          AND COALESCE(is_active,TRUE)=TRUE
        """,
        {"uid": uid},
    )
    m.products_private = _scalar_int(db, "SELECT COUNT(*) FROM products WHERE owner_id=:uid AND visibility='private'", {"uid": uid})
    m.orders = _scalar_int(db, "SELECT COUNT(*) FROM orders WHERE seller_id=:uid", {"uid": uid})
    m.earnings_cents = _scalar_int(db, "SELECT COALESCE(SUM(amount_cents),0) FROM payouts WHERE user_id=:uid", {"uid": uid})
    m.views_30d = _scalar_int(
        db,
        """
        SELECT COALESCE(COUNT(*),0)
        FROM store_views
        WHERE owner_id=:uid
          AND created_at >= (NOW() - INTERVAL '30 days')
        """,
        {"uid": uid},
    )

    response.headers["ETag"] = _etag(f"{uid}|{m.products}|{m.orders}|{m.earnings_cents}|{m.views_30d}")
    response.headers["Cache-Control"] = "private, max-age=15"
    return m


@router.post("/sellers/start", response_model=SellerOut, status_code=status.HTTP_201_CREATED)
@router.post("/seller/onboard", response_model=SellerOut, status_code=status.HTTP_201_CREATED)
def seller_start(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required (missing/invalid token or user id)")
    return _ensure_seller(db, uid)


# ─────────────────────────────────────────────────────────────
# ✅ EarnHub: Referral (kept)
# ─────────────────────────────────────────────────────────────
@router.get("/earnhub/referral/me", response_model=ReferralMeOut)
def earnhub_referral_me(
    response: Response,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    if not uid:
        response.headers["Cache-Control"] = "no-store"
        code = "SMARTBIZ-NEW"
        return ReferralMeOut(code=code, url=f"{_app_origin()}/signup?ref={code}", updated_at=_now())

    code = _get_or_create_referral(db, uid)
    url = f"{_app_origin()}/signup?ref={code}"

    response.headers["ETag"] = _etag(f"{uid}|{code}")
    response.headers["Cache-Control"] = "private, max-age=20"
    return ReferralMeOut(code=code, url=url, total_invites=0, active_referrals=0, earned_sc=0, updated_at=_now())


@router.post("/earnhub/referral/regenerate", response_model=ReferralRegenerateOut)
def earnhub_referral_regenerate(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    _ensure_earnhub_tables(db)
    u = _fetch_user_row(db, uid)
    new_code = _make_ref_code(u)
    url = f"{_app_origin()}/signup?ref={new_code}"

    if EARNHUB_AUTOCREATE:
        with suppress(Exception):
            db.execute(text("DELETE FROM earnhub_referrals WHERE user_id=:uid"), {"uid": uid})
            db.execute(text("INSERT INTO earnhub_referrals(user_id, code) VALUES(:uid,:code)"), {"uid": uid, "code": new_code})
            db.commit()

    return ReferralRegenerateOut(code=new_code, url=url, updated_at=_now())


# ─────────────────────────────────────────────────────────────
# ✅ EarnHub: Share/Proof/Sponsored/Survey/Quests (kept, minimal)
# ─────────────────────────────────────────────────────────────
@router.post("/earnhub/share/claim", response_model=ShareClaimOut, status_code=status.HTTP_201_CREATED)
def earnhub_share_claim(
    payload: ShareClaimIn,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    _ensure_earnhub_tables(db)

    url = _normalize_http_url(payload.url)
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid url")

    attempt_id = str(uuid4())

    if EARNHUB_AUTOCREATE:
        with suppress(Exception):
            db.execute(
                text(
                    """
                    INSERT INTO earnhub_share_attempts(id, user_id, share_type, platform, url, title, message, status)
                    VALUES(:id, :uid, :stype, :platform, :url, :title, :message, 'shared')
                    """
                ),
                {
                    "id": attempt_id,
                    "uid": uid,
                    "stype": payload.share_type,
                    "platform": (payload.platform or "Social")[:40],
                    "url": url,
                    "title": (payload.title or None),
                    "message": (payload.message or None),
                },
            )
            db.commit()

    return ShareClaimOut(id=attempt_id, status="shared", earned_sc=0, created_at=_now())


@router.post("/earnhub/proof", response_model=ProofOut, status_code=status.HTTP_201_CREATED)
def earnhub_submit_proof(
    payload: ProofIn,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    _ensure_earnhub_tables(db)

    proof = _normalize_http_url(payload.proof_url)
    if not proof.startswith("http://") and not proof.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid proof_url")

    pid = str(uuid4())

    if EARNHUB_AUTOCREATE:
        with suppress(Exception):
            db.execute(
                text(
                    """
                    INSERT INTO earnhub_proofs(id, user_id, mission, reference_id, proof_url, note, status)
                    VALUES(:id, :uid, :mission, :ref, :purl, :note, 'received')
                    """
                ),
                {"id": pid, "uid": uid, "mission": payload.mission, "ref": payload.reference_id, "purl": proof, "note": payload.note},
            )
            db.commit()

    return ProofOut(id=pid, mission=payload.mission, reference_id=payload.reference_id, status="received", earned_sc=0, created_at=_now())


@router.get("/earnhub/sponsored/offers", response_model=List[SponsoredOfferOut])
def earnhub_sponsored_offers(
    response: Response,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    response.headers["Cache-Control"] = "private, max-age=30"
    offers = [
        SponsoredOfferOut(
            id="spon-visit-001",
            title="Visit SmartBiz Explore",
            description="Open Explore, browse products for 30 seconds.",
            reward_sc=20,
            action="visit",
            url=f"{_app_origin()}/explore",
        ),
        SponsoredOfferOut(
            id="spon-share-002",
            title="Share a product to a friend",
            description="Share any product link to WhatsApp / Instagram and come back.",
            reward_sc=30,
            action="share",
            url=f"{_app_origin()}/earn",
        ),
    ]
    response.headers["ETag"] = _etag(f"sponsored|{len(offers)}")
    return offers


@router.post("/earnhub/survey/submit", response_model=GenericOk)
def earnhub_survey_submit(
    answers: Dict[str, Any],
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    _ensure_earnhub_tables(db)
    if EARNHUB_AUTOCREATE:
        with suppress(Exception):
            db.execute(
                text("INSERT INTO earnhub_events(user_id, event_type, ref_id, payload) VALUES(:uid,'survey_submit',NULL,:payload::jsonb)"),
                {"uid": uid, "payload": json.dumps(answers)},
            )
            db.commit()

    return GenericOk(ok=True, message="survey_received", updated_at=_now())


@router.get("/earnhub/quests", response_model=List[QuestsOut])
def earnhub_quests(
    response: Response,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    response.headers["Cache-Control"] = "private, max-age=30"
    quests = [
        QuestsOut(id="qst-profile-001", title="Complete your profile", description="Add avatar + bio + location to unlock trust.", reward_sc=25, status="open"),
        QuestsOut(id="qst-first-product-002", title="Post your first product", description="Create 1 product and publish it.", reward_sc=50, status="open"),
        QuestsOut(id="qst-first-share-003", title="Share your store once", description="Share any SmartBiz link using Share Stream mission.", reward_sc=30, status="open"),
    ]
    response.headers["ETag"] = _etag(f"quests|{len(quests)}")
    return quests


@router.post("/earnhub/quests/claim", response_model=GenericOk)
def earnhub_quest_claim(
    quest_id: str = Query(..., min_length=3, max_length=80),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    _ensure_earnhub_tables(db)
    if EARNHUB_AUTOCREATE:
        with suppress(Exception):
            db.execute(text("INSERT INTO earnhub_events(user_id, event_type, ref_id, payload) VALUES(:uid,'quest_claim',:qid,NULL)"), {"uid": uid, "qid": quest_id})
            db.commit()

    return GenericOk(ok=True, message=f"quest_claimed:{quest_id}", updated_at=_now())


# ─────────────────────────────────────────────────────────────
# ✅ EarnHub OVERVIEW (persistent, refresh-safe)
# ─────────────────────────────────────────────────────────────
@router.get("/earnhub/overview", response_model=EarnHubOverviewOut)
def earnhub_overview(
    response: Response,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    if not uid:
        response.headers["Cache-Control"] = "no-store"
        code = "SMARTBIZ-NEW"
        return EarnHubOverviewOut(
            balance=0,
            daily_cap=EARNHUB_DAILY_CAP,
            earned_today=0,
            streak={"active": True, "day": 1, "claimed": False},
            referral_code=code,
            last_claim_at=None,
            updated_at=_now().isoformat(),
        )

    try:
        bal = _get_wallet_balance(db, uid)
        daily = _get_daily(db, uid)
        st = _get_streak(db, uid)
        code = _get_or_create_referral(db, uid)
        db.commit()
    except Exception:
        with suppress(Exception):
            db.rollback()
        raise HTTPException(status_code=500, detail="EarnHub overview failed (DB)")

    last_claim_at = st.get("last_claim_at")
    last_claim_iso = last_claim_at.isoformat() if last_claim_at else None

    response.headers["Cache-Control"] = "private, max-age=10"
    response.headers["ETag"] = _etag(f"{uid}|{bal}|{daily['earned_today']}|{st.get('day')}|{st.get('claimed')}")

    return EarnHubOverviewOut(
        balance=int(bal),
        daily_cap=int(daily["daily_cap"]),
        earned_today=int(daily["earned_today"]),
        streak={"active": True, "day": int(st["day"]), "claimed": bool(st["claimed"])},
        referral_code=code,
        last_claim_at=last_claim_iso,
        updated_at=_now().isoformat(),
    )


# ─────────────────────────────────────────────────────────────
# ✅ EarnHub HISTORY (ledger) — real data for History tab
# ─────────────────────────────────────────────────────────────
@router.get("/earnhub/history", response_model=EarnHubHistoryOut)
def earnhub_history(
    response: Response,
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    if not uid:
        response.headers["Cache-Control"] = "no-store"
        return EarnHubHistoryOut(items=[], updated_at=_now().isoformat())

    items = _rows(
        db,
        """
        SELECT
          id::text AS id,
          kind,
          amount::bigint AS amount,
          COALESCE(meta,'{}'::jsonb) AS meta,
          EXTRACT(EPOCH FROM created_at)*1000 AS at_ms
        FROM earnhub_ledger
        WHERE user_id=:uid
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        {"uid": uid, "limit": limit},
    )

    out_items: List[EarnHubLedgerItem] = []
    for it in items:
        meta = it.get("meta")
        if isinstance(meta, str):
            with suppress(Exception):
                meta = json.loads(meta)
        out_items.append(
            EarnHubLedgerItem(
                id=str(it.get("id")),
                kind=str(it.get("kind")),
                amount=int(it.get("amount") or 0),
                at_ms=int(it.get("at_ms") or 0),
                meta=meta if isinstance(meta, dict) else None,
            )
        )

    response.headers["Cache-Control"] = "private, max-age=10"
    response.headers["ETag"] = _etag(f"{uid}|hist|{len(out_items)}")
    return EarnHubHistoryOut(items=out_items, updated_at=_now().isoformat())


# ─────────────────────────────────────────────────────────────
# ✅ EarnHub AD CLAIM (cooldown + cap + refresh-safe)
# ─────────────────────────────────────────────────────────────
@router.post("/earnhub/ad/claim", response_model=EarnHubActionOut)
def earnhub_ad_claim(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        daily = _get_daily(db, uid)
        cooldown_left = _ad_cooldown_left_seconds(daily)

        if cooldown_left > 0:
            return EarnHubActionOut(
                ok=False,
                earned=0,
                cooldown=int(cooldown_left),
                balance=_get_wallet_balance(db, uid),
                earned_today=int(daily["earned_today"]),
                daily_cap=int(daily["daily_cap"]),
                message=f"Cooldown active. Try again in {cooldown_left}s.",
            )

        left = _cap_left(daily["daily_cap"], daily["earned_today"])
        if left <= 0:
            return EarnHubActionOut(
                ok=False,
                earned=0,
                cooldown=EARNHUB_AD_COOLDOWN_SECONDS,
                balance=_get_wallet_balance(db, uid),
                earned_today=int(daily["earned_today"]),
                daily_cap=int(daily["daily_cap"]),
                message="Daily cap reached. Come back tomorrow.",
            )

        earned = min(EARNHUB_AD_REWARD, left)

        # Persist: daily last claim time + ledger + wallet + daily earned
        _set_last_ad_claim_at(db, uid)
        _ledger_add(db, uid, "ad", earned, meta={"day": _today_utc_date_str()})
        new_bal = _wallet_add(db, uid, earned)
        daily2 = _upsert_daily_add(db, uid, earned)
        db.commit()

        return EarnHubActionOut(
            ok=True,
            earned=int(earned),
            cooldown=EARNHUB_AD_COOLDOWN_SECONDS,
            balance=int(new_bal),
            earned_today=int(daily2["earned_today"]),
            daily_cap=int(daily2["daily_cap"]),
            message="Ad reward received",
        )

    except HTTPException:
        raise
    except Exception:
        with suppress(Exception):
            db.rollback()
        raise HTTPException(status_code=500, detail="Failed to claim ad reward")


# ─────────────────────────────────────────────────────────────
# ✅ EarnHub STREAK CLAIM (once-per-day + cap + refresh-safe)
# ─────────────────────────────────────────────────────────────
@router.post("/earnhub/streak/claim", response_model=EarnHubActionOut)
def earnhub_streak_claim(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        daily = _get_daily(db, uid)
        left = _cap_left(daily["daily_cap"], daily["earned_today"])

        st = _get_streak(db, uid)
        if left <= 0:
            return EarnHubActionOut(
                ok=False,
                earned=0,
                balance=_get_wallet_balance(db, uid),
                earned_today=int(daily["earned_today"]),
                daily_cap=int(daily["daily_cap"]),
                streak={"active": True, "day": int(st["day"]), "claimed": bool(st["claimed"])},
                message="Daily cap reached. Come back tomorrow.",
            )

        if bool(st["claimed"]):
            return EarnHubActionOut(
                ok=False,
                earned=0,
                balance=_get_wallet_balance(db, uid),
                earned_today=int(daily["earned_today"]),
                daily_cap=int(daily["daily_cap"]),
                streak={"active": True, "day": int(st["day"]), "claimed": True},
                message="Already claimed today",
            )

        reward = _streak_reward(int(st["day"]))
        earned = min(int(reward), int(left))

        # Persist streak claim for today
        today = _today_utc()
        next_day = min(7, int(st["day"]) + 1)

        db.execute(
            text(
                """
                UPDATE earnhub_streaks
                SET last_claim_date=:d,
                    last_claim_at=NOW(),
                    day=:next_day
                WHERE user_id=:uid
                """
            ),
            {"uid": uid, "d": today, "next_day": next_day},
        )

        _ledger_add(db, uid, "streak", earned, meta={"claimed_date": today.isoformat(), "prev_day": int(st["day"]), "next_day": next_day})
        new_bal = _wallet_add(db, uid, earned)
        daily2 = _upsert_daily_add(db, uid, earned)
        db.commit()

        st2 = _get_streak(db, uid)
        last_claim_at = st2.get("last_claim_at")

        return EarnHubActionOut(
            ok=True,
            earned=int(earned),
            balance=int(new_bal),
            earned_today=int(daily2["earned_today"]),
            daily_cap=int(daily2["daily_cap"]),
            streak={"active": True, "day": int(st2["day"]), "claimed": True},
            message="Streak claimed",
        )

    except HTTPException:
        raise
    except Exception:
        with suppress(Exception):
            db.rollback()
        raise HTTPException(status_code=500, detail="Failed to claim streak")


# ─────────────────────────────────────────────────────────────
# ✅ Compat endpoints (kept)
# ─────────────────────────────────────────────────────────────
@router.get("/earn/stats")
@router.get("/earnings/overview")
def earnhub_stats_compat(
    response: Response,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
):
    uid = _resolve_uid(user, authorization, x_access_token)
    response.headers["Cache-Control"] = "private, max-age=15"
    return {"ok": True, "uid": uid, "earned_sc": 0, "pending_sc": 0, "updated_at": _now().isoformat()}
