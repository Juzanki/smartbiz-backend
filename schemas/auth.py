# backend/schemas/auth.py
from __future__ import annotations

from typing import Optional, List
from datetime import datetime
from typing import Literal

# --- Pydantic v2 first, fallback to v1 (compat shims) -----------------------
_V2 = True
try:
    from pydantic import BaseModel, ConfigDict, field_validator
except Exception:  # Pydantic v1 fallback
    _V2 = False
    from pydantic import BaseModel, validator  # type: ignore
    ConfigDict = dict  # type: ignore
    def field_validator(field_name: str, *, mode: str = "after"):  # type: ignore
        # Map v2-style decorator to v1's @validator
        pre = (mode == "before")
        def deco(fn):
            return validator(field_name, pre=pre, allow_reuse=True)(fn)  # type: ignore
        return deco


# ------------------------------- MODELS -------------------------------------

class Token(BaseModel):
    """
    OAuth2 access token envelope returned to clients.
    - `access_token`: the signed JWT or opaque token
    - `token_type`: usually "bearer"
    - `expires_in`: seconds until expiry (optional)
    - `refresh_token`: optional refresh token if your flow supports it
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
            orm_mode = True
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
        if v is None:
            raise ValueError("access_token is required")
        v = str(v).strip()
        if not v:
            raise ValueError("access_token cannot be empty")
        return v

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
    exp: Optional[datetime] = None   # expiry instant (JWT 'exp'), optional
    iat: Optional[datetime] = None   # issued-at instant (JWT 'iat'), optional

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            populate_by_name=True,
            json_schema_extra={
                "example": {
                    "sub": "user_123",
                    "scopes": ["read:posts", "write:posts"],
                    "exp": "2025-12-31T23:59:59Z",
                    "iat": "2025-12-31T22:59:59Z",
                }
            },
        )
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True
            orm_mode = True
            schema_extra = {
                "example": {
                    "sub": "user_123",
                    "scopes": ["read:posts", "write:posts"],
                    "exp": "2025-12-31T23:59:59Z",
                    "iat": "2025-12-31T22:59:59Z",
                }
            }

    # --- validators ---
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
        # unique, keep order
        seen = set()
        unique = []
        for s in cleaned:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        return unique
