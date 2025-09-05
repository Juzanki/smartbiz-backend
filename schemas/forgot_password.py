# backend/schemas/forgot_password.py
from __future__ import annotations

# --- Pydantic v2 kwanza, v1 fallback (shim fupi & nyepesi) -------------------
_V2 = True
try:
    from pydantic import BaseModel, EmailStr, Field, ConfigDict, field_validator
except Exception:  # Pydantic v1
    _V2 = False
    from pydantic import BaseModel, EmailStr, Field  # type: ignore
    from pydantic import validator as _v  # type: ignore
    ConfigDict = dict  # type: ignore

    def field_validator(field_name: str, *, mode: str = "after"):  # type: ignore
        pre = (mode == "before")
        def deco(fn):
            return _v(field_name, pre=pre, allow_reuse=True)(fn)  # type: ignore
        return deco


# ------------------------------ Msingi (forbid extras) ----------------------
class _Base(BaseModel):
    if _V2:
        model_config = ConfigDict(extra="forbid")
    else:
        class Config:  # type: ignore
            extra = "forbid"


# --------------------------------- Schemas ----------------------------------
class ForgotPasswordRequest(_Base):
    email: EmailStr = Field(..., description="Account email")

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            json_schema_extra={"example": {"email": "user@example.com"}},
        )
    else:
        class Config(_Base.Config):  # type: ignore
            schema_extra = {"example": {"email": "user@example.com"}}

    @field_validator("email", mode="before")
    def _norm_email(cls, v):
        if v is None:
            raise ValueError("email is required")
        return str(v).strip().lower()


class VerifyResetCode(_Base):
    email: EmailStr
    # OTP tarakimu tu, urefu 4–8 (rahisi kwa mobile)
    code: str = Field(..., min_length=4, max_length=8, pattern=r"^\d{4,8}$")

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            json_schema_extra={"example": {"email": "user@example.com", "code": "123456"}},
        )
    else:
        class Config(_Base.Config):  # type: ignore
            schema_extra = {"example": {"email": "user@example.com", "code": "123456"}}

    @field_validator("email", mode="before")
    def _norm_email(cls, v):
        return str(v).strip().lower()

    @field_validator("code", mode="before")
    def _norm_code(cls, v):
        s = str(v or "").strip().replace(" ", "")
        if not s:
            raise ValueError("code is required")
        return s


class ResetPassword(_Base):
    email: EmailStr
    code: str = Field(..., min_length=4, max_length=8, pattern=r"^\d{4,8}$")
    new_password: str = Field(..., min_length=8, max_length=128)

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            json_schema_extra={
                "example": {
                    "email": "user@example.com",
                    "code": "123456",
                    "new_password": "StrongPass8",
                }
            },
        )
    else:
        class Config(_Base.Config):  # type: ignore
            schema_extra = {
                "example": {
                    "email": "user@example.com",
                    "code": "123456",
                    "new_password": "StrongPass8",
                }
            }

    @field_validator("email", mode="before")
    def _norm_email(cls, v):
        return str(v).strip().lower()

    @field_validator("code", mode="before")
    def _norm_code(cls, v):
        s = str(v or "").strip().replace(" ", "")
        if not s:
            raise ValueError("code is required")
        return s

    @field_validator("new_password", mode="before")
    def _norm_pwd(cls, v):
        s = str(v or "").strip()
        if " " in s:
            raise ValueError("new_password must not contain spaces")
        return s
