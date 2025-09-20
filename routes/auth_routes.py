from __future__ import annotations
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, validator
from sqlalchemy import func, or_, text
from sqlalchemy.exc import (
    DBAPIError,
    OperationalError,
    SQLAlchemyError,
    IntegrityError,
    ProgrammingError,
    StatementError,
)
from sqlalchemy.orm import Session, load_only, noload

# ──────────────────────────────────────────────────────────────────────
# DB & Models (layout-safe imports)
# ──────────────────────────────────────────────────────────────────────
try:
    from db import get_db, engine  # type: ignore
except Exception:  # pragma: no cover
    from backend.db import get_db, engine  # type: ignore

try:
    from models.user import User  # type: ignore
except Exception:  # pragma: no cover
    from backend.models.user import User  # type: ignore

# ──────────────────────────────────────────────────────────────────────
# Security helpers (hash/verify)
# ──────────────────────────────────────────────────────────────────────
try:
    # recommended: passlib/bcrypt helpers
    from backend.utils.security import verify_password, get_password_hash  # type: ignore
except Exception:  # secure fallback if helper missing (bcrypt preferred in prod)
    from passlib.hash import bcrypt

    def get_password_hash(pw: str) -> str:
        return bcrypt.hash(pw)

    def verify_password(pw: str, hashed: str) -> bool:
        try:
            return bcrypt.verify(pw, hashed)
        except Exception:
            return False

# ──────────────────────────────────────────────────────────────────────
# JWT (PyJWT)
# ──────────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    # fail-safe for first boot; strongly advise setting SECRET_KEY in prod
    SECRET_KEY = base64.urlsafe_b64encode(os.urandom(48)).decode()

JWT_ALG = os.getenv("JWT_ALG", "HS256")
ACCESS_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))      # 1 day
REFRESH_MIN = int(os.getenv("REFRESH_TOKEN_EXPIRE_MINUTES", "43200"))   # 30 days

def _now() -> datetime:
    return datetime.now(timezone.utc)

def create_access_token(claims: dict, minutes: int = ACCESS_MIN) -> str:
    now = _now()
    payload = {
        "typ": "access",
        "iss": "smartbiz-api",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
        **claims,
    }
    # normalize UUID/datetime-like
    if "sub" in payload:
        payload["sub"] = str(payload["sub"])
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALG)

def decode_token(token: str) -> Dict[str, Any]:
    return jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALG])

def _extract_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    cookie_name = os.getenv("AUTH_COOKIE_NAME", "sb_access")
    return request.cookies.get(cookie_name)

def get_current_user_from_token(request: Request) -> Dict[str, Any]:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        return decode_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# ──────────────────────────────────────────────────────────────────────
# Setup
# ──────────────────────────────────────────────────────────────────────
logger = logging.getLogger("smartbiz.auth")
router = APIRouter(prefix="/auth", tags=["Auth"])
legacy_router = APIRouter(tags=["Auth"])  # optional legacy aliases

# Cookie settings
USE_COOKIE_AUTH = os.getenv("USE_COOKIE_AUTH", "true").lower() in {"1", "true", "yes", "on", "y"}
COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "sb_access")
COOKIE_MAX_AGE = int(os.getenv("AUTH_COOKIE_MAX_AGE", str(7 * 24 * 3600)))
COOKIE_PATH = os.getenv("AUTH_COOKIE_PATH", "/")
COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "true").lower() in {"1", "true", "yes", "on", "y"}
COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "none")
COOKIE_DOMAIN = (os.getenv("AUTH_COOKIE_DOMAIN") or "").strip() or None

# Allow phone login?
ALLOW_PHONE_LOGIN = os.getenv("AUTH_LOGIN_ALLOW_PHONE", "false").lower() in {"1", "true", "yes", "on", "y"}

# Registration
def _flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on", "y"}

ALLOW_REG = (
    _flag("ALLOW_REGISTRATION", "true")
    or _flag("REGISTRATION_ENABLED", "true")
    or _flag("SIGNUP_ENABLED", "true")
    or _flag("SMARTBIZ_ALLOW_SIGNUP", "true")
)

# Rate limiter (identifier+IP)
LOGIN_RATE_MAX_PER_MIN = int(os.getenv("LOGIN_RATE_LIMIT_PER_MIN", "20"))
_RATE_WIN = 60.0
_LOGIN_BUCKET: Dict[str, List[float]] = {}

def _rate_ok(key: str) -> bool:
    now = time.time()
    q = _LOGIN_BUCKET.setdefault(key, [])
    while q and (now - q[0]) > _RATE_WIN:
        q.pop(0)
    if len(q) >= LOGIN_RATE_MAX_PER_MIN:
        return False
    q.append(now)
    return True

# ──────────────────────────────────────────────────────────────────────
# Normalizers
# ──────────────────────────────────────────────────────────────────────
_phone_digits = re.compile(r"\D+")
def _norm(s: Optional[str]) -> str: return (s or "").strip()
def _norm_email(s: Optional[str]) -> str: return _norm(s).lower()
def _norm_username(s: Optional[str]) -> str: return " ".join(_norm(s).split()).lower()
def _norm_phone(s: Optional[str]) -> str:
    s = _norm(s)
    if not s: return ""
    return "+" + _phone_digits.sub("", s) if s.startswith("+") else _phone_digits.sub("", s)

# ──────────────────────────────────────────────────────────────────────
# Duplicate check
def _precheck_duplicates(db: Session, email: str, username: str) -> None:
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email is already registered")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=409, detail="Username is already taken")

# ──────────────────────────────────────────────────────────────────────
# Register Endpoint (with duplicate check)
@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(request: Request, response: Response, db: Session = Depends(get_db)) -> AuthResponse:
    # Parse body
    body = await request.json()
    payload = RegisterRequest(**body)

    # Check if registration is allowed
    if not True:  # If registration is disabled, check here with a flag.
        raise HTTPException(status_code=403, detail="registration_disabled")

    email = payload.email.lower()
    username = payload.username.lower()

    # Normalize the phone and country code
    phone_e164 = ""  # Simulate phone number for now, if needed.

    # Duplicate check
    _precheck_duplicates(db, email, username)

    # Create user object
    user = User(
        email=email,
        username=username,
        password_hash=get_password_hash(payload.password),  # Store hashed password
        full_name=payload.full_name,
        is_active=True
    )

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
        if "phone" in msg:
            raise HTTPException(status_code=409, detail="phone_taken")
        raise HTTPException(status_code=500, detail="register_failed")

    # Generate access token
    token = create_access_token({"sub": str(user.id), "email": user.email})

    # Send the response with user info and token
    user_out = UserOut(id=str(user.id), email=user.email, username=user.username, full_name=user.full_name)
    return AuthResponse(access_token=token, token_type="bearer", user=user_out)
