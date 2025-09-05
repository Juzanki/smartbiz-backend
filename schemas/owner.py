# backend/schemas/owner.py
from __future__ import annotations

from enum import Enum
from typing import Optional

# --- Pydantic v2 kwanza, v1 fallback (shim fupi & nyepesi) -------------------
_V2 = True
try:
    from pydantic import BaseModel, EmailStr, Field, ConfigDict, field_validator, model_validator
except Exception:  # Pydantic v1
    _V2 = False
    from pydantic import BaseModel, EmailStr, Field  # type: ignore
    from pydantic import validator as _v, root_validator as _rv  # type: ignore

    class ConfigDict(dict):  # type: ignore
        pass

    def field_validator(field_name: str, *, mode: str = "after"):  # type: ignore
        pre = (mode == "before")
        def deco(fn):
            return _v(field_name, pre=pre, allow_reuse=True)(fn)  # type: ignore
        return deco

    def model_validator(*, mode: str = "after"):  # type: ignore
        def deco(fn):
            return _rv(pre=(mode == "before"), allow_reuse=True)(fn)  # type: ignore
        return deco


# ------------------------------ Base (forbid extras) ------------------------
class _Base(BaseModel):
    if _V2:
        model_config = ConfigDict(extra="forbid")
    else:
        class Config:  # type: ignore
            extra = "forbid"


# ------------------------------ Enums ---------------------------------------
class Role(str, Enum):
    owner = "owner"
    admin = "admin"
    manager = "manager"
    staff = "staff"
    user = "user"
    viewer = "viewer"


# ------------------------------ Schemas -------------------------------------
class RoleUpdateRequest(_Base):
    # Tumia MOJA tu kati ya hizi mbili kumtambulisha mtumiaji
    user_id: Optional[int] = Field(default=None, ge=1)
    email: Optional[EmailStr] = None

    role: Role
    is_active: Optional[bool] = None

    @field_validator("email", mode="before")
    def _norm_email(cls, v):
        return (str(v).strip().lower()) if v is not None else None

    @field_validator("user_id", mode="before")
    def _coerce_id(cls, v):
        if v in (None, ""):
            return None
        try:
            iv = int(v)
        except Exception:
            raise ValueError("user_id must be an integer")
        if iv < 1:
            raise ValueError("user_id must be >= 1")
        return iv

    if _V2:
        @model_validator(mode="after")
        def _one_of(self):
            # lazima iwe XOR: moja tu kati ya user_id au email
            if bool(self.user_id) ^ bool(self.email):
                return self
            raise ValueError("Provide exactly one of: user_id OR email")
    else:
        @model_validator(mode="after")  # mapped to root_validator in v1
        def _one_of(cls, values):  # type: ignore
            if bool(values.get("user_id")) ^ bool(values.get("email")):
                return values
            raise ValueError("Provide exactly one of: user_id OR email")


class AdminCreate(_Base):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128, description="Min 8 chars; no spaces")
    full_name: Optional[str] = Field(default=None, max_length=120)
    # Rahisi & nyepesi: E.164-lite (7–15 tarakimu, hiari na '+')
    phone_number: Optional[str] = Field(default=None, pattern=r"^\+?[0-9]{7,15}$")

    @field_validator("email", mode="before")
    def _norm_email(cls, v):
        return str(v).strip().lower()

    @field_validator("password", mode="before")
    def _norm_pwd(cls, v):
        s = str(v or "").strip()
        if " " in s:
            raise ValueError("password must not contain spaces")
        return s

    @field_validator("full_name", mode="before")
    def _norm_name(cls, v):
        if v is None:
            return None
        # kupunguza whitespace bila rules nzito
        s = " ".join(str(v).split())
        return s or None

    @field_validator("phone_number", mode="before")
    def _norm_phone(cls, v):
        if v is None:
            return None
        s = str(v).strip().replace(" ", "")
        return s or None
