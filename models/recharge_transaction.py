# backend/models/recharge_transaction.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import re
import datetime as dt
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
    JSON as SA_JSON,
    Numeric as SA_NUMERIC,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

# ---------- Portable JSON ----------
try:
    # Tunatumia JSON_VARIANT kutoka _types; hakuna cha kufanya hapa.
    pass
except Exception:  # pragma: no cover
    # fallback ya usalama
    pass

# ---------- Portable NUMERIC(18,2) ----------
try:
    from sqlalchemy.dialects.postgresql import NUMERIC as PG_NUMERIC  # type: ignore
    DECIMAL_TYPE = PG_NUMERIC(18, 2)
except Exception:  # pragma: no cover
    DECIMAL_TYPE = SA_NUMERIC(18, 2)

if TYPE_CHECKING:
    from .user import User
    from .payment import Payment

# --------- Enums ---------
class RechargeMethod(str, enum.Enum):
    mobile_money = "mobile_money"
    card = "card"
    bank = "bank"
    voucher = "voucher"
    wallet = "wallet"
    other = "other"


class RechargeProvider(str, enum.Enum):
    mpesa = "mpesa"
    tigopesa = "tigopesa"
    airtelmoney = "airtelmoney"
    paypal = "paypal"
    stripe = "stripe"
    pesapal = "pesapal"
    paystack = "paystack"
    bank = "bank"
    voucher = "voucher"
    test = "test"
    other = "other"


class RechargeStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"
    refunded = "refunded"
    partially_refunded = "partially_refunded"


class Environment(str, enum.Enum):
    live = "live"
    sandbox = "sandbox"


# --------- Helpers ---------
_phone_re = re.compile(r"[^\d+]")


def normalize_phone_e164(phone: Optional[str]) -> Optional[str]:
    """Normalize to near-E.164 without inferring country code."""
    if not phone:
        return None
    p = phone.strip()
    if p.startswith("+"):
        p = "+" + re.sub(r"\D", "", p[1:])
    else:
        p = re.sub(r"\D", "", p)
    return p or None


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------- Model ---------
class RechargeTransaction(Base):
    """
    Inbound account top-up / recharge record.

    - method/provider/status/env
    - amounts (gross/fees → net) + currency
    - coins_credited + rate + wallet snapshots (before/after)
    - payer identity (name/phone) & references (reference/idempotency/provider_txn_id)
    - optional link to Payment
    - retry-safe via idempotency_key
    """
    __tablename__ = "recharge_transactions"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("reference", name="uq_recharge_reference"),
        UniqueConstraint("idempotency_key", name="uq_recharge_idem"),
        UniqueConstraint("provider", "provider_txn_id", name="uq_recharge_provider_txn"),
        Index("ix_recharge_user_created", "user_id", "created_at"),
        Index("ix_recharge_status_time", "status", "created_at"),
        Index("ix_recharge_provider_method", "provider", "method"),
        Index("ix_recharge_phone", "payer_phone_e164"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Owner (beneficiary)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user: Mapped["User"] = relationship(
        "User", back_populates="recharges", passive_deletes=True, lazy="selectin"
    )

    # Optional linkage to a Payment (UUID pk)
    payment_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL"), index=True
    )
    payment: Mapped[Optional["Payment"]] = relationship("Payment", lazy="selectin")

    # Classification
    method: Mapped[RechargeMethod] = mapped_column(
        SQLEnum(RechargeMethod, name="recharge_method", native_enum=False, validate_strings=True),
        default=RechargeMethod.mobile_money, nullable=False, index=True,
    )
    provider: Mapped[RechargeProvider] = mapped_column(
        SQLEnum(RechargeProvider, name="recharge_provider", native_enum=False, validate_strings=True),
        default=RechargeProvider.mpesa, nullable=False, index=True,
    )
    status: Mapped[RechargeStatus] = mapped_column(
        SQLEnum(RechargeStatus, name="recharge_status", native_enum=False, validate_strings=True),
        default=RechargeStatus.pending, nullable=False, index=True,
    )
    environment: Mapped[Environment] = mapped_column(
        SQLEnum(Environment, name="recharge_env", native_enum=False, validate_strings=True),
        default=Environment.live, nullable=False, index=True,
    )

    # Currency & amounts
    currency: Mapped[str] = mapped_column(String(3), default="TZS", nullable=False, index=True)
    amount_gross: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    fee_total:    Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # Coins credit (SmartCoins), rate snapshot
    coins_credited:      Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    rate_coins_per_unit: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("1"))
    wallet_before:       Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    wallet_after:        Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # Payer info
    payer_name:       Mapped[Optional[str]] = mapped_column(String(160))
    payer_phone:      Mapped[Optional[str]] = mapped_column(String(32))   # raw
    payer_phone_e164: Mapped[Optional[str]] = mapped_column(String(32), index=True)

    # References / dedupe
    reference:       Mapped[Optional[str]] = mapped_column(String(100), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    provider_txn_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    request_id:      Mapped[Optional[str]] = mapped_column(String(64), index=True)
    voucher_code:    Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # Provider payloads / metadata
    provider_response: Mapped[Optional[dict]] = mapped_column(as_mutable_json(JSON_VARIANT))
    meta:              Mapped[Optional[dict]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    processing_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    succeeded_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    failed_at:     Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    canceled_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    refunded_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    expires_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # ---------- Hybrids ----------
    @hybrid_property
    def amount_net(self) -> Decimal:
        g = self.amount_gross or Decimal("0")
        f = self.fee_total or Decimal("0")
        v = g - f
        return v if v > 0 else Decimal("0")

    # ---------- Validators ----------
    @validates("currency")
    def _v_currency(self, _k: str, v: str) -> str:
        s = (v or "TZS").strip().upper()
        if len(s) != 3:
            raise ValueError("currency must be ISO3 (e.g., TZS, USD)")
        return s

    @validates("reference", "idempotency_key", "provider_txn_id", "request_id", "voucher_code", "payer_name")
    def _trim(self, _k: str, v: Optional[str]) -> Optional[str]:
        return v.strip() or None if v else None

    @validates("payer_phone")
    def _v_phone(self, _k: str, v: Optional[str]) -> Optional[str]:
        return v.strip() if v else None

    # ---------- Helpers ----------
    def normalize_phone(self) -> None:
        self.payer_phone_e164 = normalize_phone_e164(self.payer_phone)

    def mark_processing(self) -> None:
        self.status = RechargeStatus.processing
        self.processing_at = _utcnow()

    def succeed(self, *, coins: Optional[Decimal | int | float | str] = None) -> None:
        self.status = RechargeStatus.succeeded
        self.succeeded_at = _utcnow()
        if coins is not None:
            c = coins if isinstance(coins, Decimal) else Decimal(str(coins))
            self.coins_credited = max(Decimal("0"), c)

    def fail(self) -> None:
        self.status = RechargeStatus.failed
        self.failed_at = _utcnow()

    def cancel(self) -> None:
        self.status = RechargeStatus.canceled
        self.canceled_at = _utcnow()

    def refund(self, *, amount: Optional[Decimal | int | float | str] = None) -> None:
        """Mark as refunded (full/partial)."""
        self.refunded_at = _utcnow()
        self.status = RechargeStatus.refunded if amount is None else RechargeStatus.partially_refunded
        if amount is not None:
            # weka kwenye meta kwa kumbukumbu
            self.meta = {**(self.meta or {}), "refund_amount": str(amount)}

    def apply_rate_and_snapshot(self) -> None:
        """Hesabu coins_credited na wallet_after ukitumia rate + amount_net + wallet_before."""
        net = self.amount_net
        rate = self.rate_coins_per_unit or Decimal("1")
        credited = (net * rate) if net > 0 else Decimal("0")
        self.coins_credited = credited
        self.wallet_after = (self.wallet_before or Decimal("0")) + credited

    def link_payment(self, payment: "Payment") -> None:
        """Funga na rekodi ya Payment (ikiwa ipo)."""
        self.payment_id = payment.id
        # unaweza kuweka meta ya chanzo
        self.meta = {**(self.meta or {}), "linked_payment_id": payment.id}

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<RechargeTransaction id={self.id} user={self.user_id} {self.status} "
            f"{self.amount_gross} {self.currency} {self.provider}>"
        )

    __table_args__ = (
        CheckConstraint("length(currency) = 3", name="ck_recharge_currency_iso3"),
        CheckConstraint("amount_gross >= 0 AND fee_total >= 0", name="ck_recharge_amounts_nonneg"),
        CheckConstraint(
            "coins_credited >= 0 AND wallet_before >= 0 AND wallet_after >= 0",
            name="ck_recharge_wallet_nonneg",
        ),
    )


# ---------- Normalization hooks ----------
@listens_for(RechargeTransaction, "before_insert")
def _recharge_before_insert(_m, _c, target: RechargeTransaction) -> None:  # pragma: no cover
    if target.currency:
        target.currency = target.currency.strip().upper()
    if target.reference:
        target.reference = target.reference.strip()
    target.normalize_phone()
    # auto-calc wallet_after if not provided
    if (target.wallet_after or Decimal("0")) == Decimal("0"):
        target.apply_rate_and_snapshot()


@listens_for(RechargeTransaction, "before_update")
def _recharge_before_update(_m, _c, target: RechargeTransaction) -> None:  # pragma: no cover
    if target.currency:
        target.currency = target.currency.strip().upper()
    if target.reference:
        target.reference = target.reference.strip()
    target.normalize_phone()
