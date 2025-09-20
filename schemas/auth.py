from __future__ import annotations
from typing import Optional, List, Union
from datetime import datetime
from typing import Literal
from uuid import UUID
import re

# --- Pydantic v2 first, fallback to v1 (compat shims) -----------------------
_V2 = True
try:
    from pydantic import BaseModel, ConfigDict, field_validator, field_serializer, EmailStr
except Exception:  # Pydantic v1 fallback
    _V2 = False
    from pydantic import BaseModel, validator, EmailStr  # type: ignore
    ConfigDict = dict  # type: ignore

    def field_validator(field_name: str, *, mode: str = "after"):  # type: ignore
        pre = (mode == "before")
        def deco(fn):
            return validator(field_name, pre=pre, allow_reuse=True)(fn)  # type: ignore
        return deco

    def field_serializer(*_a, **_k):  # type: ignore
        def deco(fn):
            return fn
        return deco

# ------------------------------- HELPERS -------------------------------------
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,50}$")

def _ensure_non_empty_str(v: object, name: str) -> str:
    if v is None:
        raise ValueError(f"{name} is required")
    s = str(v).strip()
    if not s:
        raise ValueError(f"{name} cannot be empty")
    return s

# ------------------------------- MODELS -------------------------------------

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

    # --- validators ---
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
    - `sub`: subject/user id (e.g., user UUID)
    - `scopes`: list of granted scopes/permissions (lowercase, unique)
    - `exp` / `iat`: optional JWT-style timestamps
    """
    sub: Optional[str] = None
    scopes: List[str] = []
    exp: Optional[datetime] = None
    iat: Optional[datetime] = None

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            populate_by_name=True,
            json_schema_extra={
                "example": {
                    "sub": "b6f7a2e8-0a6b-4a5e-90c3-7a9a6b8a1234",
                    "scopes": ["read:items", "write:items"],
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
                    "sub": "b6f7a2e8-0a6b-4a5e-90c3-7a9a6b8a1234",
                    "scopes": ["read:items", "write:items"],
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
        cleaned = []
        for s in v:
            s = str(s).strip()
            if not s:
                continue
            cleaned.append(s.lower())
        seen = set()
        unique = []
        for s in cleaned:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        return unique


# ---------- Requests from clients ----------

class RegisterRequest(BaseModel):
    email: EmailStr
    username: str
    password: str
    full_name: Optional[str] = None

    if _V2:
        model_config = ConfigDict(extra="forbid", populate_by_name=True)
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True

    @field_validator("username", mode="before")
    def _validate_username(cls, v):
        v = _ensure_non_empty_str(v, "username")
        if not _USERNAME_RE.match(v):
            raise ValueError("username must be 3–50 chars; letters, numbers, _ . - only")
        return v

    @field_validator("password", mode="before")
    def _validate_password(cls, v):
        v = _ensure_non_empty_str(v, "password")
        if len(v) < 8:
            raise ValueError("password must be at least 8 characters")
        if not (re.search(r"[A-Z]", v) and re.search(r"[a-z]", v) and re.search(r"[0-9]", v)):
            raise ValueError("password must contain upper, lower, and number")
        return v

    @field_validator("full_name", mode="before")
    def _normalize_full_name(cls, v):
        if v is None:
            return v
        v = str(v).strip()
        return v or None


class LoginRequest(BaseModel):
    identifier: str
    password: str

    if _V2:
        model_config = ConfigDict(extra="forbid", populate_by_name=True)
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True

    @field_validator("identifier", mode="before")
    def _id_required(cls, v):
        return _ensure_non_empty_str(v, "identifier")

    @field_validator("password", mode="before")
    def _pwd_required(cls, v):
        return _ensure_non_empty_str(v, "password")


# ---------- Outbound to clients ----------

class UserOut(BaseModel):
    id: Union[str, UUID]
    email: EmailStr
    username: str
    full_name: Optional[str] = None
    is_active: Optional[bool] = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    if _V2:
        model_config = ConfigDict(extra="ignore", populate_by_name=True)

        # Serialize UUID → str
        @field_serializer("id")
        def _ser_id(self, v):
            return str(v) if isinstance(v, (UUID, str)) else str(v)

        # Serialize datetimes → ISO 8601
        @field_serializer("created_at", "updated_at")
        def _ser_dt(self, v):
            return v.isoformat() if isinstance(v, datetime) else v
    else:
        class Config:  # type: ignore
            extra = "ignore"
            allow_population_by_field_name = True
            orm_mode = True

        @field_validator("id")
        def _v1_id_to_str(cls, v):
            return str(v)

        @field_validator("created_at", "updated_at")
        def _v1_dt_iso(cls, v):
            return v  # v1 will be handled by jsonable_encoder in routes


class AuthResponse(Token):
    """Convenient envelope for /auth/login and /auth/register responses."""
    user: UserOut

    if _V2:
        model_config = ConfigDict(extra="forbid", populate_by_name=True)
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True
            orm_mode = True
