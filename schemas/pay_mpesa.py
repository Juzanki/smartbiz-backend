# backend/schemas/pay_mpesa.py
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Optional

# --- Pydantic v2 kwanza, v1 fallback (shim fupi & nyepesi) -------------------
_V2 = True
try:
    from pydantic import BaseModel, Field, ConfigDict, field_validator
except Exception:  # Pydantic v1
    _V2 = False
    from pydantic import BaseModel, Field  # type: ignore
    from pydantic import validator as _v  # type: ignore

    class ConfigDict(dict):  # type: ignore
        pass

    def field_validator(field_name: str, *, mode: str = "after"):  # type: ignore
        pre = (mode == "before")
        def deco(fn):
            return _v(field_name, pre=pre, allow_reuse=True)(fn)  # type: ignore
        return deco


# ------------------------------ Base (forbid extras) ------------------------
class _Base(BaseModel):
    if _V2:
        model_config = ConfigDict(extra="forbid")
    else:
        class Config:  # type: ignore
            extra = "forbid"


# ------------------------------ Helpers -------------------------------------
_phone_re = re.compile(r"^\+?[0-9]{9,15}$")
_txn_re = re.compile(r"^[A-Za-z0-9]{6,32}$")
_ref_re  = re.compile(r"^[A-Za-z0-9._-]{1,30}$")  # nyepesi, bila nafasi

def _to_decimal_2(v) -> Decimal:
    """Parse→Decimal(2dp). Nyepesi & salama kwa amounts."""
    try:
        d = Decimal(str(v))
    except Exception:
        raise ValueError("amount must be a number")
    if d <= 0:
        raise ValueError("amount must be > 0")
    # quantize 2 d.p. (half up) — rahisi kwa sarafu nyingi
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _clean_phone(v: str) -> str:
    s = str(v or "")
    s = s.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not _phone_re.match(s):
        raise ValueError("invalid phone_number format")
    return s


# ------------------------------ Schemas -------------------------------------
class PaymentRequest(_Base):
    amount: Decimal = Field(..., description="Payment amount (>0), 2dp")
    phone_number: str = Field(..., description="E.164-lite, 9–15 digits, '+' optional")
    account_reference: str = Field(
        ..., min_length=1, max_length=30,
        description="Reference shown on statement (1–30, A-Z a-z 0-9 . _ -)"
    )
    currency: Optional[str] = Field(default="TZS", pattern=r"^[A-Z]{3}$")
    idempotency_key: Optional[str] = Field(default=None, max_length=64)

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            json_schema_extra={
                "example": {
                    "amount": "5000",
                    "phone_number": "+255712345678",
                    "account_reference": "ORDER-1023",
                    "currency": "TZS",
                    "idempotency_key": "8c8f0a1e-7e0c-4c9e-9a2f-2d9d0a0d1e1f"
                }
            },
        )
    else:
        class Config(_Base.Config):  # type: ignore
            schema_extra = {
                "example": {
                    "amount": "5000",
                    "phone_number": "+255712345678",
                    "account_reference": "ORDER-1023",
                    "currency": "TZS",
                    "idempotency_key": "8c8f0a1e-7e0c-4c9e-9a2f-2d9d0a0d1e1f"
                }
            }

    # ---- validators (nyepesi) ----
    @field_validator("amount", mode="before")
    def _amount_parse(cls, v):
        return _to_decimal_2(v)

    @field_validator("phone_number", mode="before")
    def _phone_norm(cls, v):
        return _clean_phone(v)

    @field_validator("account_reference", mode="before")
    def _ref_norm(cls, v):
        s = str(v or "").strip()
        if not s or not _ref_re.match(s):
            raise ValueError("invalid account_reference")
        return s

    @field_validator("currency", mode="before")
    def _currency_norm(cls, v):
        if v is None:
            return "TZS"
        s = str(v).strip().upper()
        if len(s) != 3:
            raise ValueError("currency must be 3 letters")
        return s

    @field_validator("idempotency_key", mode="before")
    def _idemp_trim(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None


class PaymentResponse(_Base):
    success: bool
    message: str
    transaction_id: Optional[str] = Field(default=None)
    amount: Optional[Decimal] = Field(default=None)
    phone_number: Optional[str] = Field(default=None)

    # nyepesi: hakikisha kama zikitumwa, ziko safi
    @field_validator("transaction_id", mode="before")
    def _txn_ok(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        if not _txn_re.match(s):
            raise ValueError("invalid transaction_id")
        return s

    @field_validator("amount", mode="before")
    def _amt_ok(cls, v):
        if v is None:
            return None
        return _to_decimal_2(v)

    @field_validator("phone_number", mode="before")
    def _phone_ok(cls, v):
        if v is None:
            return None
        return _clean_phone(v)


class ConfirmMpesaRequest(_Base):
    transaction_id: str = Field(..., min_length=6, max_length=32)
    amount: Decimal
    phone_number: str

    # validators
    @field_validator("transaction_id", mode="before")
    def _txn_norm(cls, v):
        s = str(v or "").strip()
        if not _txn_re.match(s):
            raise ValueError("invalid transaction_id")
        return s

    @field_validator("amount", mode="before")
    def _amount_parse(cls, v):
        return _to_decimal_2(v)

    @field_validator("phone_number", mode="before")
    def _phone_norm(cls, v):
        return _clean_phone(v)
