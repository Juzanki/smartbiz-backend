# backend/schemas/auth.py
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional, List, Union, Literal
from uuid import UUID

# ---------------- Pydantic v2 first, fallback to v1 (compat shims) ----------------
_V2 = True
try:
    from pydantic import (
        BaseModel,
        ConfigDict,
        EmailStr,
        Field,
        field_validator,
        field_serializer,
        model_validator,
    )
except Exception:  # Pydantic v1 fallback
    _V2 = False
    from pydantic import BaseModel, EmailStr, Field, root_validator as model_validator  # type: ignore
    ConfigDict = dict  # type: ignore

    # v1 shims
    def field_validator(_name: str, *, mode: str = "after"):  # type: ignore
        pre = (mode == "before")
        def deco(fn):
            return fn if not pre else fn
        return deco

    def field_serializer(*_a, **_k):  # type: ignore
        def deco(fn):
            return fn
        return deco


# ------------------------------- HELPERS -------------------------------------
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,50}$")
MIN_PASSWORD_LEN = int(os.getenv("MIN_PASSWORD_LEN", "6"))  # openapi yako ilionyesha min 6

def _ensure_non_empty_str(v: object, name: str) -> str:
    if v is None:
        raise ValueError(f"{name} is required")
    s = str(v).strip()
    if not s:
        raise ValueError(f"{name} cannot be empty")
    return s


# =============================== SCHEMAS =====================================

# ─── Token shells ────────────────────────────────────────────────────────────
class Token(BaseModel):
    """
    OAuth2 access token envelope.
    """
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: Optional[int] = None
    refresh_token: Optional[str] = None

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            populate_by_name=True,
            json_schema_extra={
                "example": {
                    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                    "token_type": "bearer",
                    "expires_in": 3600
                }
            },
        )
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True
            schema_extra = {
                "example": {
                    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                    "token_type": "bearer",
                    "expires_in": 3600
                }
            }

    @field_validator("access_token", mode="before")
    def _strip_token(cls, v):
        return _ensure_non_empty_str(v, "access_token")

    @field_validator("expires_in")
    def _positive_exp(cls, v):
        if v is not None and int(v) <= 0:
            raise ValueError("expires_in must be a positive integer")
        return v


class TokenData(BaseModel):
    """
    Claims decoded from a token or carried in the auth context.
      - sub: subject/user id
      - scopes: unique lowercased scopes
      - exp/iat: optional timestamps
    """
    sub: Optional[str] = None
    scopes: List[str] = Field(default_factory=list)
    exp: Optional[datetime] = None
    iat: Optional[datetime] = None

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            populate_by_name=True,
            json_schema_extra={
                "example": {
                    "sub": "42",
                    "scopes": ["read:profile", "write:posts"],
                    "exp": "2025-12-31T23:59:59Z",
                    "iat": "2025-12-31T22:59:59Z",
                }
            },
        )
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True
            schema_extra = {
                "example": {
                    "sub": "42",
                    "scopes": ["read:profile", "write:posts"],
                    "exp": "2025-12-31T23:59:59Z",
                    "iat": "2025-12-31T22:59:59Z",
                }
            }

    @field_validator("scopes", mode="before")
    def _normalize_scopes(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        cleaned: List[str] = []
        for s in v:
            s = str(s).strip()
            if s:
                cleaned.append(s.lower())
        # unique & preserve order
        seen, out = set(), []
        for s in cleaned:
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out


# ─── Requests from clients ───────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: EmailStr
    username: str
    password: str
    full_name: Optional[str] = None

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            populate_by_name=True,
            json_schema_extra={
                "example": {
                    "email": "alice@example.com",
                    "username": "alice_01",
                    "password": "pa55Word",
                    "full_name": "Alice Doe"
                }
            },
        )
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True
            schema_extra = {
                "example": {
                    "email": "alice@example.com",
                    "username": "alice_01",
                    "password": "pa55Word",
                    "full_name": "Alice Doe"
                }
            }

    @field_validator("username", mode="before")
    def _validate_username(cls, v):
        v = _ensure_non_empty_str(v, "username")
        if not _USERNAME_RE.match(v):
            raise ValueError("username must be 3–50 chars; letters, numbers, _ . - only")
        return v

    @field_validator("password", mode="before")
    def _validate_password(cls, v):
        v = _ensure_non_empty_str(v, "password")
        if len(v) < MIN_PASSWORD_LEN:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")
        return v

    @field_validator("full_name", mode="before")
    def _normalize_full_name(cls, v):
        if v is None:
            return v
        v = str(v).strip()
        return v or None


class LoginRequest(BaseModel):
    """
    Inapokea **jozi yoyote** kati ya:
      - identifier
      - email
      - username
      - email_or_username
    na ku-normalize kuwa `identifier` + `password`.
    """
    identifier: str
    password: str

    if _V2:
        model_config = ConfigDict(
            extra="allow",              # ruhusu majina mengine ili tuyakusanye (email/username/...)
            populate_by_name=True,
            json_schema_extra={
                "examples": [
                    {"identifier": "you@example.com", "password": "secret"},
                    {"email_or_username": "you@example.com", "password": "secret"},
                    {"username": "youuser", "password": "secret"},
                    {"email": "you@example.com", "password": "secret"},
                ]
            },
        )
    else:
        class Config:  # type: ignore
            extra = "allow"
            allow_population_by_field_name = True
            schema_extra = {
                "examples": [
                    {"identifier": "you@example.com", "password": "secret"},
                    {"email_or_username": "you@example.com", "password": "secret"},
                    {"username": "youuser", "password": "secret"},
                    {"email": "you@example.com", "password": "secret"},
                ]
            }

    @model_validator(mode="before")
    def _coalesce_identifier(cls, values):
        # values inaweza kuwa dict ya raw payload
        if not isinstance(values, dict):
            return values
        ident = values.get("identifier") or values.get("email_or_username") \
                or values.get("username") or values.get("email")
        if not ident:
            # acha Pydantic atoe 422 badala ya 500
            return values
        values["identifier"] = str(ident).strip()
        return values

    @field_validator("identifier", mode="before")
    def _id_required(cls, v):
        return _ensure_non_empty_str(v, "identifier")

    @field_validator("password", mode="before")
    def _pwd_required(cls, v):
        return _ensure_non_empty_str(v, "password")


# ─── Outbound to clients ─────────────────────────────────────────────────────
class UserOut(BaseModel):
    # OpenAPI yako ya sasa inaonyesha id: integer ⇒ tumia int hapa
    id: int
    email: EmailStr
    username: Optional[str] = None
    full_name: Optional[str] = None
    is_active: Optional[bool] = True
    # hzi mbili ni za bonus—si lazima zirudishwe; zikiwa hazipo, sawa.
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    if _V2:
        model_config = ConfigDict(
            extra="ignore",
            populate_by_name=True,
            from_attributes=True,  # pydantic v2 ORM mode
        )

        # Datetimes → ISO-8601
        @field_serializer("created_at", "updated_at")
        def _ser_dt(self, v):
            return v.isoformat() if isinstance(v, datetime) else v
    else:
        class Config:  # type: ignore
            extra = "ignore"
            allow_population_by_field_name = True
            orm_mode = True


class AuthResponse(Token):
    """Envelope kwa /auth/login, /auth/signin, /auth/register."""
    user: UserOut

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            populate_by_name=True,
            json_schema_extra={
                "example": {
                    "access_token": "eyJhbGciOi...",
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "user": {
                        "id": 1,
                        "email": "you@example.com",
                        "username": "youuser",
                        "full_name": "You User",
                        "is_active": True
                    }
                }
            },
        )
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True
            schema_extra = {
                "example": {
                    "access_token": "eyJhbGciOi...",
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "user": {
                        "id": 1,
                        "email": "you@example.com",
                        "username": "youuser",
                        "full_name": "You User",
                        "is_active": True
                    }
                }
            }
