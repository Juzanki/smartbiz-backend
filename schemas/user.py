# backend/schemas/user.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
User schemas (Pydantic v2-first, v1 fallback)
- UserBase:   common user fields
- UserCreate: payload for creating a user (server must hash 'password')
- UserUpdate: partial update (all optional)
- UserOut:    safe response object

Kipengele: normalization ya email/username/full_name + validators imara.
"""

import os
import re
from datetime import datetime
from typing import Optional

# ───────────────── Pydantic v2 first, v1 fallback (compat shims) ─────────────
_P2 = True
try:
    from pydantic import (
        BaseModel,
        EmailStr,
        Field,
        ConfigDict,
        field_validator,
        field_serializer,
    )
except Exception:  # Pydantic v1
    _P2 = False
    from pydantic import BaseModel, EmailStr, Field, validator as field_validator  # type: ignore
    ConfigDict = dict  # type: ignore

    def field_serializer(*_a, **_k):  # type: ignore
        def deco(fn):
            return fn
        return deco

# ───────────────────────────── Helpers / constants ───────────────────────────
_USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,50}$")  # lowercase letters, digits, _ . -
MIN_PASSWORD_LEN = int(os.getenv("MIN_PASSWORD_LEN", "8"))

def _strip_or_none(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None

def _ensure_non_empty_str(v: object, name: str) -> str:
    if v is None:
        raise ValueError(f"{name} is required")
    s = str(v).strip()
    if not s:
        raise ValueError(f"{name} cannot be empty")
    return s

# ───────────────────────────────── Base ──────────────────────────────────────
class UserBase(BaseModel):
    email: EmailStr = Field(..., max_length=255)
    username: Optional[str] = Field(None, max_length=80, description="Lowercase handle; 3–50 chars, [a-z0-9_.-]")
    full_name: Optional[str] = Field(None, max_length=120)
    is_active: bool = True

    if _P2:
        model_config = ConfigDict(extra="ignore")
    else:
        class Config:  # type: ignore
            extra = "ignore"

    # ── normalization ──
    @field_validator("email", mode="before")
    def _v_email_lower(cls, v):
        v = _ensure_non_empty_str(v, "email")
        return v.lower()

    @field_validator("username", mode="before")
    def _v_username_norm(cls, v):
        v = _strip_or_none(v)
        if v is None:
            return v
        v = v.lower()
        if not _USERNAME_RE.match(v):
            raise ValueError("username must be 3–50 chars; allowed: a-z 0-9 _ . - (lowercase)")
        return v

    @field_validator("full_name", mode="before")
    def _v_full_name_trim(cls, v):
        return _strip_or_none(v)

# ───────────────────────── Create / Update / Out ─────────────────────────────
class UserCreate(UserBase):
    """
    Plain password from client; server MUST hash before storing.
    """
    password: str = Field(
        ...,
        min_length=MIN_PASSWORD_LEN,
        max_length=128,
        description="Raw password (hash on server)",
        examples=["S3cur3Pass", "pa55Word!"],
    )

    @field_validator("password", mode="before")
    def _v_password_basic(cls, v):
        v = _ensure_non_empty_str(v, "password")
        if len(v) < MIN_PASSWORD_LEN:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")
        return v


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = Field(None, max_length=255)
    username: Optional[str] = Field(None, max_length=80, description="Lowercase handle; 3–50 chars, [a-z0-9_.-]")
    full_name: Optional[str] = Field(None, max_length=120)
    is_active: Optional[bool] = None

    if _P2:
        model_config = ConfigDict(extra="ignore")
    else:
        class Config:  # type: ignore
            extra = "ignore"

    # normalization consistent with base
    @field_validator("email", mode="before")
    def _vu_email_lower(cls, v):
        return v.lower() if v else v

    @field_validator("username", mode="before")
    def _vu_username_norm(cls, v):
        v = _strip_or_none(v)
        if v is None:
            return v
        v = v.lower()
        if not _USERNAME_RE.match(v):
            raise ValueError("username must be 3–50 chars; allowed: a-z 0-9 _ . - (lowercase)")
        return v

    @field_validator("full_name", mode="before")
    def _vu_full_name_trim(cls, v):
        return _strip_or_none(v)


class UserOut(BaseModel):
    id: int
    email: EmailStr
    username: Optional[str] = None
    full_name: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    if _P2:
        model_config = ConfigDict(from_attributes=True, extra="ignore")

        # Datetimes → ISO 8601 (only when present)
        @field_serializer("created_at", "updated_at")
        def _ser_dt(self, v):
            return v.isoformat() if isinstance(v, datetime) else v
    else:
        class Config:  # type: ignore
            orm_mode = True
            extra = "ignore"

# ────────────────────────────── Exports ──────────────────────────────────────
__all__ = ["UserBase", "UserCreate", "UserUpdate", "UserOut"]
