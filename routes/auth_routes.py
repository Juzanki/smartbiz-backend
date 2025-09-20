 # backend/routes/auth_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_
from sqlalchemy.exc import (
    IntegrityError, SQLAlchemyError, DBAPIError, OperationalError, ProgrammingError, StatementError
)
from sqlalchemy.orm import Session, load_only, noload

# ──────────────── DB wiring (layout-safe) ────────────────
try:
    from db import get_db  # type: ignore
except Exception:  # pragma: no cover
    from backend.db import get_db  # type: ignore

try:
    from models.user import User  # type: ignore
except Exception:  # pragma: no cover
    from backend.models.user import User  # type: ignore

# ──────────────── Schemas & Security ────────────────
try:
    from backend.schemas.auth import (
        RegisterRequest, LoginRequest, UserOut, AuthResponse
    )
except Exception:
    # Fallback ikiwa path zako ni tofauti
    from schemas.auth import (
        RegisterRequest, LoginRequest, UserOut, AuthResponse
    )

from backend.utils.security import (
    verify_password, get_password_hash, create_access_token
)

logger = logging.getLogger("smartbiz.auth")

# ──────────────── Config / Flags ────────────────
def _flag(name: str, default: str = "true") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "on", "y"}

USE_COOKIE_AUTH = _flag("USE_COOKIE_AUTH", "true")
COOKIE_NAME     = os.getenv("AUTH_COOKIE_NAME", "sb_access")
COOKIE_MAX_AGE  = int(os.getenv("AUTH_COOKIE_MAX_AGE", str(7 * 24 * 3600)))
COOKIE_PATH     = os.getenv("AUTH_COOKIE_PATH", "/")
COOKIE_SECURE   = _flag("AUTH_COOKIE_SECURE", "true")
COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "none")  # "none" for Netlify↔Render cross-site
COOKIE_DOMAIN   = (os.getenv("AUTH_COOKIE_DOMAIN") or "").strip() or None

ALLOW_PHONE_LOGIN = _flag("AUTH_LOGIN_ALLOW_PHONE", "false")
ALLOW_REG = any([
    _flag("ALLOW_REGISTRATION", "true"),
    _flag("REGISTRATION_ENABLED", "true"),
    _flag("SIGNUP_ENABLED", "true"),
    _flag("SMARTBIZ_ALLOW_SIGNUP", "true"),
])

# ──────────────── Rate limiter (best-effort, in-memory) ────────────────
LOGIN_RATE_MAX_PER_MIN  = int(os.getenv("LOGIN_RATE_LIMIT_PER_MIN", "20"))
SIGNUP_RATE_MAX_PER_MIN = int(os.getenv("SIGNUP_RATE_LIMIT_PER_MIN", "10"))
_RATE_WIN = 60.0
_LOGIN_BUCKET: Dict[str, List[float]] = {}
_SIGNUP_BUCKET: Dict[str, List[float]] = {}

def _bucket_ok(bucket: Dict[str, List[float]], key: str, max_per_min: int) -> bool:
    now = time.time()
    arr = bucket.setdefault(key, [])
    # drop old
    while arr and (now - arr[0]) > _RATE_WIN:
        arr.pop(0)
    if len(arr) >= max_per_min:
        return False
    arr.append(now)
    return True

def _rate_ok_or_429_login(identifier: str, ip: str) -> None:
    key = f"login::{identifier.lower()}::{ip}"
    if not _bucket_ok(_LOGIN_BUCKET, key, LOGIN_RATE_MAX_PER_MIN):
        raise HTTPException(status_code=429, detail="too_many_login_attempts")

def _rate_ok_or_429_signup(identifier: str, ip: str) -> None:
    key = f"signup::{identifier.lower()}::{ip}"
    if not _bucket_ok(_SIGNUP_BUCKET, key, SIGNUP_RATE_MAX_PER_MIN):
        raise HTTPException(status_code=429, detail="too_many_signup_attempts")

# ──────────────── Normalizers ────────────────
_phone_digits = re.compile(r"\D+")

def _norm(s: Optional[str]) -> str:
    return (s or "").strip()

def _norm_email(s: Optional[str]) -> str:
    return _norm(s).lower()

def _norm_username(s: Optional[str]) -> str:
    # keep display case, but duplicates are checked case-insensitively
    return " ".join(_norm(s).split())

def _norm_cc(s: Optional[str]) -> str:
    s = _norm(s)
    return s.lstrip("+") if s else ""

def _norm_local(s: Optional[str]) -> str:
    s = _norm(s)
    return s.lstrip("0") if s else ""

def _e164(cc: str, local: str) -> str:
    if not cc or not local:
        return ""
    return f"+{cc}{local}"

# ──────────────── Cookie helpers ────────────────
def _set_auth_cookie(resp: Response, token: str) -> None:
    if not USE_COOKIE_AUTH:
        return
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        path=COOKIE_PATH,
        domain=COOKIE_DOMAIN,
        secure=COOKIE_SECURE,
        httponly=True,
        samesite=COOKIE_SAMESITE,  # "none" for cross-site
    )

# ──────────────── Router ────────────────
router = APIRouter(prefix="/auth", tags=["Auth"])

# Public “whoami” (quick check)
@router.get("/whoami")
def whoami(request: Request) -> Dict[str, Any]:
    token = request.headers.get("authorization") or request.headers.get("Authorization")
    cookie = request.cookies.get(COOKIE_NAME)
    return {"has_auth_header": bool(token), "has_auth_cookie": bool(cookie)}

# Diags: versions (useful for Render)
@router.get("/_diag")
def auth_diag():
    import pkg_resources, importlib
    def ver(pkg):
        try:
            return pkg_resources.get_distribution(pkg).version
        except Exception:
            return "n/a"
    out = {
        "deps": {
            "fastapi": ver("fastapi"),
            "starlette": ver("starlette"),
            "pydantic": ver("pydantic"),
            "passlib": ver("passlib"),
            "bcrypt": getattr(importlib.import_module("bcrypt"), "__version__", "n/a"),
            "python-jose": ver("python-jose"),
            "cryptography": ver("cryptography"),
        }
    }
    return out

# ──────────────── LOGIN ────────────────
# Tutapokea JSON (kipaumbele) au form. JSON inapaswa kutumia LoginRequest (identifier + password).
@router.post("/login", response_model=AuthResponse)
async def login(request: Request, response: Response, db: Session = Depends(get_db)) -> JSONResponse:
    # Parse body
    ctype = _norm(request.headers.get("content-type")).lower()
    identifier = ""
    password = ""

    if "application/json" in ctype:
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            data = LoginRequest(**(body or {}))
            identifier = _norm(data.identifier)
            password = _norm(data.password)
        except Exception:
            # legacy support: {email, username, phone, password}
            identifier = _norm((body or {}).get("identifier") or (body or {}).get("email") or (body or {}).get("username"))
            password = _norm((body or {}).get("password"))
    else:
        # Form fallback
        form = await request.form()
        identifier = _norm(form.get("identifier") or form.get("email") or form.get("username") or form.get("phone"))
        password = _norm(form.get("password"))

    if not identifier or not password:
        raise HTTPException(status_code=422, detail="missing_credentials")

    ip = request.client.host if request.client else "0.0.0.0"
    _rate_ok_or_429_login(identifier, ip)

    # Build query conditions dynamically by available columns
    cols = {c.key for c in User.__table__.columns}  # type: ignore
    conds: List[Any] = []

    # email
    if "email" in cols:
        conds.append(func.lower(User.email) == _norm_email(identifier))

    # username variants
    for cand in ("username", "user_name", "handle"):
        if cand in cols:
            conds.append(func.lower(getattr(User, cand)) == identifier.lower())
            break

    # phone (optional)
    if ALLOW_PHONE_LOGIN:
        digits = _phone_digits.sub("", identifier)
        for cand in ("phone_e164", "msisdn", "phone", "phone_number"):
            if cand in cols:
                if cand == "phone_e164":
                    e = digits if digits.startswith("+") else ("+" + digits if digits else "")
                    if e:
                        conds.append(getattr(User, cand) == e)
                else:
                    if digits:
                        conds.append(getattr(User, cand) == digits)
                break

    if not conds:
        # No way to look user up
        raise HTTPException(status_code=401, detail="invalid_credentials")

    # Choose password column
    pwd_col = None
    for name in ("password_hash", "hashed_password", "password"):
        if name in cols:
            pwd_col = name
            break
    if not pwd_col:
        raise HTTPException(status_code=500, detail="password_storage_not_configured")

    # Query minimal fields
    load_names = ["id", pwd_col]
    for n in ("email", "full_name", "role", "username", "user_name", "handle",
              "phone_e164", "phone_number", "is_active", "preferred_language",
              "created_at", "updated_at"):
        if n in cols:
            load_names.append(n)
    only_attrs = [getattr(User, n) for n in dict.fromkeys(load_names).keys()]  # preserve order, unique

    try:
        user = (
            db.query(User)
            .options(noload("*"), load_only(*only_attrs))  # type: ignore[arg-type]
            .filter(or_(*conds))
            .limit(1)
            .first()
        )
    except (ProgrammingError, DBAPIError, OperationalError, SQLAlchemyError, StatementError) as e:
        logger.exception("login query failed: %s", e)
        raise HTTPException(status_code=500, detail="login_failed")

    if not user:
        # slow-hash dummy to equalize timing
        try:
            verify_password("dummy", "$2b$12$abcdefghijklmnopqrstuv/abcdefghijklmnopqrstuvwxyz12")
        except Exception:
            pass
        raise HTTPException(status_code=401, detail="invalid_credentials")

    hashed_value = getattr(user, pwd_col, None)
    if not (hashed_value and verify_password(password, hashed_value)):
        raise HTTPException(status_code=401, detail="invalid_credentials")

    if hasattr(user, "is_active") and getattr(user, "is_active") is False:
        raise HTTPException(status_code=403, detail="account_disabled")

    token = create_access_token(
        data={"sub": str(getattr(user, "id")), "email": getattr(user, "email", None)}
    )
    _set_auth_cookie(response, token)

    # Build UserOut
    username_value: Optional[str] = None
    for cand in ("username", "user_name", "handle"):
        if hasattr(user, cand):
            username_value = getattr(user, cand, None)
            if username_value:
                break

    user_out = UserOut(
        id=str(getattr(user, "id")),
        email=getattr(user, "email", None),
        username=username_value or "",
        full_name=getattr(user, "full_name", None),
        is_active=getattr(user, "is_active", True),
        created_at=getattr(user, "created_at", None),
        updated_at=getattr(user, "updated_at", None),
    )

    out = AuthResponse(access_token=token, token_type="bearer", user=user_out)
    return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder(out))

# ──────────────── REGISTER ────────────────
class _RegisterForm(BaseModel):
    # hii ni kwa fallback ya form; JSON tumia RegisterRequest moja kwa moja
    email: EmailStr
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8, max_length=256)
    full_name: Optional[str] = None
    phone_country_code: Optional[str] = None
    phone_number: Optional[str] = None
    preferred_language: Optional[str] = "en"
    agree_terms: bool = True

def _has_col(name: str) -> bool:
    return name in {c.key for c in User.__table__.columns}  # type: ignore

def _precheck_duplicates(db: Session, email: str, username: str, phone_e164: str) -> None:
    if _has_col("email"):
        if db.query(User.id).filter(func.lower(User.email) == email).first():
            raise HTTPException(status_code=409, detail="email_taken")

    uname_field = None
    for cand in ("username", "user_name", "handle"):
        if _has_col(cand):
            uname_field = cand
            if db.query(User.id).filter(func.lower(getattr(User, cand)) == username.lower()).first():
                raise HTTPException(status_code=409, detail="username_taken")
            break

    if phone_e164:
        if _has_col("phone_e164"):
            if db.query(User.id).filter(getattr(User, "phone_e164") == phone_e164).first():
                raise HTTPException(status_code=409, detail="phone_taken")
        elif _has_col("phone_country_code") and _has_col("phone_number"):
            # if storing pair instead of e164
            pass

@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
@router.post("/signup",   response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(request: Request, response: Response, db: Session = Depends(get_db)) -> JSONResponse:
    if not ALLOW_REG:
        raise HTTPException(status_code=403, detail="registration_disabled")

    ip = request.client.host if request.client else "0.0.0.0"

    # Parse body
    ctype = _norm(request.headers.get("content-type")).lower()
    payload: Union[RegisterRequest, _RegisterForm]
    try:
        if "application/json" in ctype:
            body = await request.json()
            payload = RegisterRequest(**(body or {}))
        else:
            form = await request.form()
            payload = _RegisterForm(
                email=form.get("email"),
                username=form.get("username"),
                password=form.get("password"),
                full_name=form.get("full_name"),
                phone_country_code=form.get("phone_country_code"),
                phone_number=form.get("phone_number"),
                preferred_language=form.get("preferred_language") or "en",
                agree_terms=(str(form.get("agree_terms")).lower() in {"1", "true", "yes", "on"}),
            )
    except Exception as e:
        logger.debug("register parse error: %s", e)
        raise HTTPException(status_code=422, detail="invalid_payload")

    # Rate limit
    _rate_ok_or_429_signup(_norm_email(payload.email), ip)

    # Normalize
    email = _norm_email(payload.email)
    username = _norm_username(payload.username)
    if hasattr(payload, "agree_terms") and getattr(payload, "agree_terms") is False:
        raise HTTPException(status_code=422, detail="terms_not_accepted")

    cc = _norm_cc(getattr(payload, "phone_country_code", None))
    local = _norm_local(getattr(payload, "phone_number", None))
    phone_e = _e164(cc, local) if (cc and local) else ""

    # Duplicate checks
    _precheck_duplicates(db, email, username, phone_e)

    # Build user
    user = User()
    if _has_col("email"):
        setattr(user, "email", email)

    # pick password column
    pwd_col = None
    for name in ("password_hash", "hashed_password", "password"):
        if _has_col(name):
            pwd_col = name; break
    if not pwd_col:
        raise HTTPException(status_code=500, detail="password_storage_not_configured")

    setattr(user, pwd_col, get_password_hash(payload.password))

    # username variants
    for cand in ("username", "user_name", "handle"):
        if _has_col(cand):
            setattr(user, cand, username)
            break

    if _has_col("full_name"):
        setattr(user, "full_name", getattr(payload, "full_name", None))

    if _has_col("preferred_language"):
        setattr(user, "preferred_language", getattr(payload, "preferred_language", "en"))

    # phone fields (if present)
    if phone_e and _has_col("phone_e164"):
        setattr(user, "phone_e164", phone_e)
    if cc and _has_col("phone_country_code"):
        setattr(user, "phone_country_code", cc)
    if local and _has_col("phone_number"):
        setattr(user, "phone_number", local)

    if _has_col("is_active"):
        setattr(user, "is_active", True)
    if _has_col("agreed_terms_at"):
        setattr(user, "agreed_terms_at", func.now())

    # Persist
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError as e:
        db.rollback()
        msg = str(getattr(e.orig, "diag", None) or e).lower()
        if "email" in msg:
            raise HTTPException(status_code=409, detail="email_taken")
        if "user" in msg or "name" in msg or "handle" in msg:
            raise HTTPException(status_code=409, detail="username_taken")
        if "phone" in msg or "msisdn" in msg:
            raise HTTPException(status_code=409, detail="phone_taken")
        raise HTTPException(status_code=500, detail="register_failed")
    except (ProgrammingError, DBAPIError, OperationalError, SQLAlchemyError, StatementError):
        db.rollback()
        raise HTTPException(status_code=500, detail="register_failed")

    # Token + response
    token = create_access_token(
        data={"sub": str(getattr(user, "id")), "email": getattr(user, "email", None)}
    )
    _set_auth_cookie(response, token)

    username_value: Optional[str] = None
    for cand in ("username", "user_name", "handle"):
        if hasattr(user, cand):
            username_value = getattr(user, cand, None)
            if username_value:
                break

    user_out = UserOut(
        id=str(getattr(user, "id")),
        email=getattr(user, "email", None),
        username=username_value or "",
        full_name=getattr(user, "full_name", None),
        is_active=getattr(user, "is_active", True),
        created_at=getattr(user, "created_at", None),
        updated_at=getattr(user, "updated_at", None),
    )
    out = AuthResponse(access_token=token, token_type="bearer", user=user_out)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=jsonable_encoder(out))
