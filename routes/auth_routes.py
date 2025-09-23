# backend/routes/auth_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import jwt  # PyJWT
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# ──────────────────────────────────────────────────────────────────────
# DB & Models (layout-safe imports)
# ──────────────────────────────────────────────────────────────────────
try:
    from db import get_db  # type: ignore
except Exception:  # pragma: no cover
    from backend.db import get_db  # type: ignore

try:
    from models.user import User  # type: ignore
except Exception:  # pragma: no cover
    from backend.models.user import User  # type: ignore

# ──────────────────────────────────────────────────────────────────────
# Security helpers (hash/verify)
# ──────────────────────────────────────────────────────────────────────
try:
    from backend.utils.security import verify_password, get_password_hash  # type: ignore
except Exception:
    try:
        from utils.security import verify_password, get_password_hash  # type: ignore
    except Exception:
        # Fallback ya ndani: passlib[bcrypt]
        from passlib.context import CryptContext

        _pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

        def get_password_hash(pw: str) -> str:
            return _pwd_ctx.hash(pw)

        def verify_password(pw: str, hashed: str) -> bool:
            try:
                return _pwd_ctx.verify(pw, hashed)
            except Exception:
                return False

# ──────────────────────────────────────────────────────────────────────
# JWT (PyJWT)
# ──────────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY") or os.getenv("JWT_SECRET")
if not SECRET_KEY:
    # ⚠️ Dev fallback tu. Kwenye production weka SECRET_KEY/ JWT_SECRET kwenye env.
    SECRET_KEY = base64.urlsafe_b64encode(os.urandom(48)).decode()

JWT_ALG = os.getenv("JWT_ALG", os.getenv("JWT_ALGORITHM", "HS256"))
ACCESS_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))  # default 60 min


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _secs(minutes: int) -> int:
    return int(timedelta(minutes=minutes).total_seconds())


def _ensure_str(x: Any) -> str:
    if isinstance(x, bytes):
        return x.decode("utf-8")
    return str(x)


def create_access_token(claims: dict, minutes: int = ACCESS_MIN) -> Tuple[str, int]:
    now = _now()
    exp = now + timedelta(minutes=minutes)
    payload = {
        "typ": "access",
        "iss": "smartbiz-api",
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        **claims,
    }
    if "sub" in payload:
        payload["sub"] = str(payload["sub"])
    token = jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALG)
    token = _ensure_str(token)
    return token, _secs(minutes)


def decode_token(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALG])
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_or_expired_token"
        ) from e


# ──────────────────────────────────────────────────────────────────────
# Config & Flags
# ──────────────────────────────────────────────────────────────────────
logger = logging.getLogger("smartbiz.auth")
router = APIRouter(prefix="/auth", tags=["auth"])


def _flag(name: str, default: str = "true") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "on", "y"}


USE_COOKIE_AUTH = _flag("USE_COOKIE_AUTH", "true")
COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "sb_access")
COOKIE_MAX_AGE = int(os.getenv("AUTH_COOKIE_MAX_AGE", str(7 * 24 * 3600)))  # 7 days
COOKIE_PATH = os.getenv("AUTH_COOKIE_PATH", "/")
COOKIE_SECURE = _flag("AUTH_COOKIE_SECURE", "true")
COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "none")  # 'lax' | 'strict' | 'none'
COOKIE_DOMAIN = (os.getenv("AUTH_COOKIE_DOMAIN") or "").strip() or None

ALLOW_PHONE_LOGIN = _flag("AUTH_LOGIN_ALLOW_PHONE", "false")

ALLOW_REG = (
    _flag("ALLOW_REGISTRATION", "true")
    or _flag("REGISTRATION_ENABLED", "true")
    or _flag("SIGNUP_ENABLED", "true")
    or _flag("SMARTBIZ_ALLOW_SIGNUP", "true")
)

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


def _norm(s: Optional[str]) -> str:
    return (s or "").strip()


def _norm_email(s: Optional[str]) -> str:
    return _norm(s).lower()


def _norm_username(s: Optional[str]) -> str:
    return " ".join(_norm(s).split()).lower()


def _norm_phone(s: Optional[str]) -> str:
    s = _norm(s)
    if not s:
        return ""
    return "+" + _phone_digits.sub("", s) if s.startswith("+") else _phone_digits.sub("", s)


# ──────────────────────────────────────────────────────────────────────
# User password field helpers
# ──────────────────────────────────────────────────────────────────────
def _get_user_password_hash(u) -> Optional[str]:
    for attr in ("password_hash", "hashed_password", "password"):
        if hasattr(u, attr):
            return getattr(u, attr)
    return None


def _set_user_password_hash(u, plain: str) -> None:
    hashed = get_password_hash(plain)
    if hasattr(u, "password_hash"):
        u.password_hash = hashed
    elif hasattr(u, "hashed_password"):
        u.hashed_password = hashed
    elif hasattr(u, "password"):
        u.password = hashed
    else:
        raise RuntimeError(
            "User model missing password field (expected: password_hash / hashed_password / password)"
        )


# ──────────────────────────────────────────────────────────────────────
# Pydantic Schemas
# ──────────────────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=6, max_length=128)
    full_name: Optional[str] = Field(default=None, max_length=120)


class LoginAny(BaseModel):
    # Inaruhusu: email_or_username | email | identifier
    email_or_username: Optional[str] = None
    email: Optional[EmailStr] = None
    identifier: Optional[str] = None
    password: str

    # Pydantic v1
    @classmethod
    def unify(cls, data: Dict[str, Any]) -> "LoginAny":
        ident = (
            (data.get("email_or_username") or data.get("email") or data.get("identifier") or "")
            .strip()
        )
        return cls(email_or_username=ident, password=str(data.get("password") or ""))


class UserOut(BaseModel):
    id: int
    email: EmailStr
    username: Optional[str] = None
    full_name: Optional[str] = None
    is_active: Optional[bool] = True

    @staticmethod
    def from_orm_user(u) -> "UserOut":
        return UserOut(
            id=int(getattr(u, "id")),
            email=getattr(u, "email"),
            username=getattr(u, "username", None),
            full_name=getattr(u, "full_name", None),
            is_active=getattr(u, "is_active", True),
        )


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


# ──────────────────────────────────────────────────────────────────────
# Query helpers
# ──────────────────────────────────────────────────────────────────────
def _find_user_by_identifier(db: Session, ident: str):
    ident = ident.strip()
    phone_norm = _norm_phone(ident) if ALLOW_PHONE_LOGIN else None

    query = db.query(User)
    conds = [User.email == ident, User.username == ident]
    for phone_field in ("phone", "phone_number", "msisdn"):
        if hasattr(User, phone_field) and phone_norm:
            conds.append(getattr(User, phone_field) == phone_norm)
            break
    return query.filter(or_(*conds)).first()


def _precheck_duplicates(db: Session, email: str, username: str) -> None:
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="email_taken")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=409, detail="username_taken")


# ──────────────────────────────────────────────────────────────────────
# Token / Cookie utilities
# ──────────────────────────────────────────────────────────────────────
def _maybe_set_cookie(response: Response, token: str) -> None:
    if not USE_COOKIE_AUTH:
        return
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,  # 'none' in prod needs HTTPS
        max_age=COOKIE_MAX_AGE,
        path=COOKIE_PATH,
        domain=COOKIE_DOMAIN,
    )


def _extract_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return request.cookies.get(COOKIE_NAME) if USE_COOKIE_AUTH else None


# ──────────────────────────────────────────────────────────────────────
# Dependencies
# ──────────────────────────────────────────────────────────────────────
def get_current_user(request: Request, db: Session = Depends(get_db)):
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_token")
    data = decode_token(token)
    if data.get("typ") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token_type")
    sub = data.get("sub")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token_payload")

    try:
        uid = int(sub)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_sub")

    user = None
    try:
        user = db.get(User, uid)  # SA 1.4+
    except Exception:
        user = db.query(User).get(uid)  # legacy

    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user_not_found")
    if hasattr(user, "is_active") and not getattr(user, "is_active"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user_inactive")
    return user


# ──────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────
@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, response: Response, db: Session = Depends(get_db)):
    if not ALLOW_REG:
        raise HTTPException(status_code=403, detail="registration_disabled")

    email = _norm_email(payload.email)
    username = _norm_username(payload.username)

    _precheck_duplicates(db, email, username)

    user = User(
        email=email,
        username=username,
        full_name=payload.full_name,
        is_active=True if hasattr(User, "is_active") else None,
    )
    _set_user_password_hash(user, payload.password)

    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError as e:
        db.rollback()
        msg = (str(e) or "").lower()
        if "email" in msg:
            raise HTTPException(status_code=409, detail="email_taken")
        if any(k in msg for k in ("user", "name", "username", "handle")):
            raise HTTPException(status_code=409, detail="username_taken")
        logger.exception("register_failed")
        raise HTTPException(status_code=500, detail="register_failed")

    token, expires_in = create_access_token({"sub": user.id, "email": user.email, "username": user.username})
    _maybe_set_cookie(response, token)

    return AuthResponse(
        access_token=_ensure_str(token),
        expires_in=expires_in,
        user=UserOut.from_orm_user(user),
    )


@router.post("/login", response_model=AuthResponse)
@router.post("/signin", response_model=AuthResponse)
async def login(request: Request, response: Response, db: Session = Depends(get_db)):
    """
    Inakubali:
      - JSON:   {"email_or_username": "...", "password": "..."}  (pia inakubali "email" au "identifier")
      - FORM:   username=...&password=...  (application/x-www-form-urlencoded/multipart)
    """
    try:
        # Soma body kulingana na Content-Type bila kutumia Depends
        ct = (request.headers.get("content-type") or "").lower()
        ident = ""
        password = ""

        if "application/json" in ct:
            try:
                raw = await request.json()
            except Exception:
                raw = {}
            data = LoginAny.unify(dict(raw or {}))
            ident = (data.email_or_username or "").strip()
            password = str(data.password or "")
        elif "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
            form = await request.form()
            ident = (
                form.get("username")
                or form.get("email_or_username")
                or form.get("email")
                or form.get("identifier")
                or ""
            ).strip()
            password = str(form.get("password") or "")
        else:
            # Jaribu JSON kama fallback
            try:
                raw = await request.json()
                data = LoginAny.unify(dict(raw or {}))
                ident = (data.email_or_username or "").strip()
                password = str(data.password or "")
            except Exception:
                pass

        if not ident or not password:
            raise HTTPException(status_code=400, detail="missing_credentials")

        # Rate limit (per IP + preview ya ident)
        ip = (request.client.host if request.client else "unknown").strip()
        rl_key = f"{ip}|{ident[:24]}"
        if not _rate_ok(rl_key):
            raise HTTPException(status_code=429, detail="too_many_requests")

        user = _find_user_by_identifier(db, ident)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")

        hashed = _get_user_password_hash(user)
        if not hashed or not verify_password(password, hashed):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")

        if hasattr(user, "is_active") and not getattr(user, "is_active"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user_inactive")

        token, expires_in = create_access_token({"sub": user.id, "email": user.email, "username": user.username})
        token = _ensure_str(token)
        _maybe_set_cookie(response, token)

        return AuthResponse(
            access_token=token,
            expires_in=expires_in,
            user=UserOut.from_orm_user(user),
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("login_crashed")
        raise HTTPException(status_code=500, detail="server_error_login")


@router.get("/me", response_model=UserOut)
def me(current=Depends(get_current_user)):
    return UserOut.from_orm_user(current)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response):
    if USE_COOKIE_AUTH:
        response.delete_cookie(
            key=COOKIE_NAME,
            path=COOKIE_PATH,
            domain=COOKIE_DOMAIN,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
