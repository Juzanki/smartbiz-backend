# -*- coding: utf-8 -*-
from __future__ import annotations
"""
User schemas (Pydantic v1/v2 compatible).

- UserBase: common user fields
- UserCreate: payload for creating a user (password in; server will hash)
- UserUpdate: partial update (all optional)
- UserOut: response object (safe to return)

NB: Imports come from ._compat so both Pydantic v1 and v2 work.
"""

from typing import Optional
from datetime import datetime
from ._compat import BaseModel, Field, ConfigDict, P2, EmailStr


# ===================== Base =====================
class UserBase(BaseModel):
    email: EmailStr = Field(..., max_length=255)
    full_name: Optional[str] = Field(None, max_length=120)
    is_active: bool = True

    if P2:
        model_config = ConfigDict(extra="ignore")
    else:
        class Config:  # type: ignore
            extra = "ignore"


# ===================== Create / Update / Out =====================
class UserCreate(UserBase):
    # Plain password from client; your service must hash before storing.
    password: str = Field(..., min_length=8, max_length=128, description="Raw password (hash on server)")


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = Field(None, max_length=255)
    full_name: Optional[str] = Field(None, max_length=120)
    is_active: Optional[bool] = None

    if P2:
        model_config = ConfigDict(extra="ignore")
    else:
        class Config:  # type: ignore
            extra = "ignore"


class UserOut(BaseModel):
    id: int
    email: EmailStr
    full_name: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    if P2:
        model_config = ConfigDict(from_attributes=True, extra="ignore")
    else:
        class Config:  # type: ignore
            orm_mode = True
            extra = "ignore"


__all__ = ["UserBase", "UserCreate", "UserUpdate", "UserOut"]
