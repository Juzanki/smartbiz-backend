# backend/auth.py
from __future__ import annotations

import os
import time
from typing import Optional, Dict, Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

# ── DB & User model (layout-safe)
try:
    from db import get_db  # type: ignore
except Exception:
    from backend.db import get_db  # type: ignore

try:
    from models.user import User  # type: ignore
except Exception:
    from backend.models.user import User  # type: ignore

# ── JWT lib (PyJWT au python-jose — tutatumia chochote kilichopo)
_jwt = None
try:
    import jwt as _pyjwt  # PyJWT
    _jwt = _pyjwt
except Exception:
    try:
        from jose import jwt as _josejwt  # python-jose
        _jwt = _josejwt
    except Exception:
        _jwt = None  # fallback ya dev tu (haita-sign)

def _now() -> int:
    return int(time.time())

# ── Env
JWT_SECRET = os.getenv("JWT_SECRET") or os.getenv("SECRET_KEY") or "change-me-in-prod"
JWT_ALG = os.getenv("JWT_ALG", "HS256")
JWT_AUD = os.getenv("JWT_AUD")  # optional
ACCESS_EXPIRE_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRES_MINUTES", str(60 * 24)))  # default 1 day

# Cookie options (zingatie ulivyo-set kwenye auth_routes)
COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "sb_access")

# ── Security scheme kwa Bearer
bearer_scheme = HTTPBearer(auto_error=False)

def create_access_token(data: Dict[str, Any], minutes: int = ACCESS_EXPIRE_MIN) -> str:
    """
    Tengeneza JWT yenye iat/nbf/exp. 'data' inapaswa kuwa na 'sub' (user id kama string).
    """
    if _jwt is None:
        # Fallback isiyosainiwa (dev only) — epuka production!
        import base64, json as _json
        payload = data.copy()
        now = _now()
        payload.setdefault("iat", now)
        payload.setdefault("nbf", now)
        payload.setdefault("exp", now + minutes * 60)
        return base64.urlsafe_b64encode(_json.dumps(payload).encode()).decode()

    now = _now()
    payload = {**data, "iat": now, "nbf": now, "exp": now + minutes * 60}
    if JWT_AUD:
        payload["aud"] = JWT_AUD
    return _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)  # PyJWT & jose interface sawa

def _decode_token(token: str) -> Dict[str, Any]:
    if _jwt is None:
        # Fallback ya dev: decode bila verify
        import base64, json as _json
        try:
            body = token.split(".")[1] if "." in token else token
            pad = "=" * ((4 - len(body) % 4) % 4)
            return _json.loads(base64.urlsafe_b64decode(body + pad).decode())
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")
    try:
        options = {"verify_aud": bool(JWT_AUD)}
        # kwa PyJWT & jose: audience param inafanya kazi pale JWT_AUD ipo
        return _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG], audience=JWT_AUD, options=options)  # type: ignore[arg-type]
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

def _get_token_from_request(request: Request) -> Optional[str]:
    # 1) Authorization: Bearer <token>
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    # 2) Cookie
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        return cookie
    return None

def _fetch_user(db: Session, user_id: str) -> User:
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=401, detail="User not found")
    return u

async def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> User:
    """
    Dependency: rudisha User kutoka kwenye JWT (Bearer au cookie).
    """
    token = creds.credentials if (creds and creds.scheme and creds.scheme.lower() == "bearer") else _get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = _decode_token(token)
    sub = str(payload.get("sub") or "")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token: sub missing")

    # Kama decoder ya fallback imetumika, hakiki exp manually
    exp = payload.get("exp")
    if isinstance(exp, int) and exp < _now():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")

    return _fetch_user(db, sub)
