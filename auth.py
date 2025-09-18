# backend/auth.py
from __future__ import annotations

import os
import time
import uuid
from typing import Optional, Dict, Any, TypedDict

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

# ──────────────────────────── DB & User model (layout-safe) ────────────────────────────
try:
    from db import get_db  # type: ignore
except Exception:
    from backend.db import get_db  # type: ignore

try:
    from models.user import User  # type: ignore
except Exception:
    from backend.models.user import User  # type: ignore


# ──────────────────────────── Env & defaults ────────────────────────────
ENVIRONMENT = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "production").lower()
JWT_SECRET = os.getenv("JWT_SECRET") or os.getenv("SECRET_KEY") or "change-me-in-prod"
JWT_ALG = os.getenv("JWT_ALG", "HS256")
JWT_ISS = os.getenv("JWT_ISS")  # optional
JWT_AUD = os.getenv("JWT_AUD")  # optional
ACCESS_EXPIRE_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRES_MINUTES", os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440")))
JWT_LEEWAY_SEC = int(os.getenv("JWT_LEEWAY_SEC", "30"))  # inaruhusu clock skew kidogo

# Cookie options (zingatie pia ulivyo-set kwenye routes zako)
COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "sb_access")
COOKIE_SECURE = (os.getenv("AUTH_COOKIE_SECURE", "true").lower() in {"1", "true", "yes", "on"})
COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "none")
COOKIE_PATH = os.getenv("AUTH_COOKIE_PATH", "/")
COOKIE_DOMAIN = (os.getenv("AUTH_COOKIE_DOMAIN") or "").strip() or None


# ──────────────────────────── JWT libs (PyJWT / python-jose) ────────────────────────────
_jwt = None
_is_pyjwt = False
try:
    import jwt as _pyjwt  # PyJWT
    _jwt = _pyjwt
    _is_pyjwt = True
except Exception:
    try:
        from jose import jwt as _josejwt  # python-jose
        _jwt = _josejwt
        _is_pyjwt = False
    except Exception:
        _jwt = None  # NO JWT LIB: hatutaruhusu production kufanya hivi


def _now() -> int:
    return int(time.time())


class Claims(TypedDict, total=False):
    sub: str
    email: str
    iat: int
    nbf: int
    exp: int
    iss: str
    aud: str
    jti: str
    role: str
    name: str


# ──────────────────────────── Cookie helpers ────────────────────────────
def set_auth_cookie(resp: Response, token: str, max_age: int) -> None:
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        expires=max_age,
        path=COOKIE_PATH,
        domain=COOKIE_DOMAIN,
        secure=COOKIE_SECURE,
        httponly=True,
        samesite=COOKIE_SAMESITE,  # "none" kwa SPA cross-site
    )


def clear_auth_cookie(resp: Response) -> None:
    resp.delete_cookie(key=COOKIE_NAME, path=COOKIE_PATH, domain=COOKIE_DOMAIN)


# ──────────────────────────── Token creation / decoding ────────────────────────────
def create_access_token(data: Dict[str, Any], minutes: int = ACCESS_EXPIRE_MIN) -> str:
    """
    Tengeneza JWT yenye iat/nbf/exp (+hiari iss/aud/jti). 'data' inapaswa kuwa na 'sub' (string).
    """
    if _jwt is None:
        # Usiruhusu production bila JWT lib
        if ENVIRONMENT == "production":
            raise RuntimeError("JWT library (PyJWT or python-jose) not installed in production")
        # Dev fallback (SIO SALAMA): token isiyo-signed ili kuepuka kukwama wakati wa dev
        import base64, json as _json
        now = _now()
        payload: Claims = {**data}  # type: ignore[assignment]
        payload["iat"] = now
        payload["nbf"] = now
        payload["exp"] = now + minutes * 60
        if JWT_ISS:
            payload["iss"] = JWT_ISS
        if JWT_AUD:
            payload["aud"] = JWT_AUD
        payload["jti"] = uuid.uuid4().hex
        return base64.urlsafe_b64encode(_json.dumps(payload).encode()).decode()

    now = _now()
    payload: Claims = {**data}  # type: ignore[assignment]
    if "sub" in payload:
        payload["sub"] = str(payload["sub"])
    payload["iat"] = now
    payload["nbf"] = now
    payload["exp"] = now + minutes * 60
    payload["jti"] = uuid.uuid4().hex
    if JWT_ISS:
        payload["iss"] = JWT_ISS
    if JWT_AUD:
        payload["aud"] = JWT_AUD

    # PyJWT & jose zina interface sawa kwa encode
    return _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)  # type: ignore[arg-type]


def decode_token(token: str) -> Claims:
    """
    Decode & verify JWT (signature, exp, nbf, iss/aud ikiwa zimetolewa).
    Inaruhusu leeway kidogo (JWT_LEEWAY_SEC) kwa clock skew.
    """
    if _jwt is None:
        if ENVIRONMENT == "production":
            raise HTTPException(status_code=401, detail="Token verification unavailable")
        # Dev fallback: Base64 payload only
        import base64, json as _json
        try:
            body = token.split(".")[1] if "." in token else token
            pad = "=" * ((4 - len(body) % 4) % 4)
            claims: Claims = _json.loads(base64.urlsafe_b64decode(body + pad).decode())
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")
        # manual exp/nbf checks
        now = _now()
        if int(claims.get("nbf", 0)) - JWT_LEEWAY_SEC > now:
            raise HTTPException(status_code=401, detail="Token not yet valid")
        if now > int(claims.get("exp", 0)) + JWT_LEEWAY_SEC:
            raise HTTPException(status_code=401, detail="Token expired")
        if JWT_ISS and claims.get("iss") not in (None, JWT_ISS):
            raise HTTPException(status_code=401, detail="Invalid token issuer")
        if JWT_AUD and claims.get("aud") not in (None, JWT_AUD):
            raise HTTPException(status_code=401, detail="Invalid token audience")
        return claims

    try:
        # PyJWT na jose hutumia options + leeway tofauti kidogo
        options = {
            "verify_signature": True,
            "verify_exp": True,
            "verify_nbf": True,
            "verify_iat": True,
            "verify_iss": bool(JWT_ISS),
            "verify_aud": bool(JWT_AUD),
        }
        kwargs: Dict[str, Any] = dict(algorithms=[JWT_ALG], options=options, leeway=JWT_LEEWAY_SEC)
        if JWT_ISS:
            kwargs["issuer"] = JWT_ISS
        if JWT_AUD:
            kwargs["audience"] = JWT_AUD

        claims: Claims = _jwt.decode(token, JWT_SECRET, **kwargs)  # type: ignore[call-arg]
        return claims
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# ──────────────────────────── Token extraction ────────────────────────────
def _get_token_from_request(request: Request) -> Optional[str]:
    # PREFER cookie (SPA cross-site), kisha Authorization header
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        return cookie
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return None


# ──────────────────────────── User fetch helper ────────────────────────────
def _fetch_user(db: Session, user_id: str) -> User:
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=401, detail="User not found")
    return u


# ──────────────────────────── Public helpers (easy mode kwa routes zako) ────────────────────────────
def issue_user_token(user_id: str, email: Optional[str] = None, extra: Optional[Dict[str, Any]] = None,
                     minutes: int = ACCESS_EXPIRE_MIN) -> str:
    """
    Rahisisha: tengeneza token kwa user_id + hiari email/extra claims.
    """
    claims: Dict[str, Any] = {"sub": str(user_id)}
    if email:
        claims["email"] = email
    if extra:
        claims.update(extra)
    return create_access_token(claims, minutes=minutes)


# ──────────────────────────── FastAPI dependencies ────────────────────────────
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_claims(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Claims:
    """
    Dependency: rudisha **claims** zilizothibitishwa (JWT).
    Ukiwa na Bearer utaipokea, la sivyo itatumia cookie.
    """
    token = None
    if creds and (creds.scheme or "").lower() == "bearer":
        token = creds.credentials
    token = token or _get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return decode_token(token)


async def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> User:
    """
    Dependency: rudisha **User** kutoka JWT (Bearer au cookie).
    """
    claims = await get_current_claims(request, creds)  # reuse logic
    sub = str(claims.get("sub") or "")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token: sub missing")
    return _fetch_user(db, sub)
