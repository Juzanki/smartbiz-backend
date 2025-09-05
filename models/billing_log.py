# backend/models/billing_log.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING, Any, Dict

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.event import listens_for

from sqlalchemy import Numeric as SA_NUMERIC
try:
    from sqlalchemy.dialects.postgresql import NUMERIC as PG_NUMERIC  # type: ignore
    DECIMAL_TYPE = PG_NUMERIC(18, 2)
except Exception:  # pragma: no cover
    DECIMAL_TYPE = SA_NUMERIC(18, 2)

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

if TYPE_CHECKING:
    from .user import User


# -------------------- Enums --------------------
class BillingStatus(str, enum.Enum):
    success  = "success"
    failed   = "failed"
    refunded = "refunded"
    pending  = "pending"
    reversed = "reversed"


class BillingType(str, enum.Enum):
    subscription   = "subscription"
    recharge       = "recharge"
    purchase       = "purchase"
    gift           = "gift"
    withdrawal_fee = "withdrawal_fee"
    refund         = "refund"
    adjustment     = "adjustment"
    other          = "other"


class BillingProvider(str, enum.Enum):
    mpesa       = "mpesa"
    tigopesa    = "tigopesa"
    airtelmoney = "airtelmoney"
    halopesa    = "halopesa"
    paypal      = "paypal"
    stripe      = "stripe"
    internal    = "internal"
    unknown     = "unknown"


# -------------------- Helpers --------------------
def _money(v: Decimal | int | float | str) -> Decimal:
    """Normalize → Decimal(18,2) with bankers rounding."""
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# -------------------- Model --------------------
class BillingLog(Base):
    """
    Kumbukumbu za miamala (recharge/purchase/subscription/gifts/refunds/adjustments).

    Uainishaji:
      - `amount_gross`  — kiasi kikubwa (positive=ingress, negative=refund)
      - `fee`, `tax`    — zisizo hasi
      - `amount_net`    — hybrid: gross - fee - tax (inaweza kuwa negative kwa refund)
      - `reference`     — unique (kwa upana wa jedwali), hifadhi kumbukumbu ya mtoa huduma
      - `idempotency_key` — unique per-user ili kuzuia duplicates

    Portability: Postgres ↔ SQLite (JSON_VARIANT / NUMERIC handled).
    """
    __tablename__ = "billing_logs"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Nani
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user: Mapped["User"] = relationship(
        "User",
        backref="billing_logs",
        foreign_keys=lambda: [BillingLog.user_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # Nini
    type: Mapped[BillingType] = mapped_column(
        SQLEnum(BillingType, name="billing_type", native_enum=False, validate_strings=True),
        nullable=False,
        index=True,
    )
    status: Mapped[BillingStatus] = mapped_column(
        SQLEnum(BillingStatus, name="billing_status", native_enum=False, validate_strings=True),
        default=BillingStatus.success,
        nullable=False,
        index=True,
    )
    currency: Mapped[str] = mapped_column(String(8), default="TZS", nullable=False, index=True)

    # Vitambulisho
    reference: Mapped[Optional[str]] = mapped_column(String(120), unique=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    provider: Mapped[BillingProvider] = mapped_column(
        SQLEnum(BillingProvider, name="billing_provider", native_enum=False, validate_strings=True),
        default=BillingProvider.unknown,
        nullable=False,
        index=True,
    )
    provider_ref: Mapped[Optional[str]] = mapped_column(String(120), index=True)   # tx id from gateway

    # Kiasi (NUMERIC 18,2)
    amount_gross: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    fee:          Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    tax:          Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    description: Mapped[Optional[str]] = mapped_column(String(255))

    # JSON portable: MutableDict.as_mutable ili updates zisajiliwe
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        MutableDict.as_mutable(JSON_VARIANT), nullable=True
    )

    # Lini
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def amount_net(self) -> Decimal:
        return (self.amount_gross or Decimal("0")) - (self.fee or Decimal("0")) - (self.tax or Decimal("0"))

    # ---------- Validators / Normalizers ----------
    @validates("currency")
    def _norm_currency(self, _k: str, v: str) -> str:
        v = (v or "").strip().upper()
        if len(v) < 2 or len(v) > 8:
            raise ValueError("currency must be 2–8 chars (ISO like TZS, USD)")
        return v

    @validates("reference", "idempotency_key", "provider_ref", "description")
    def _trim_120_255(self, key: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if key in {"reference", "idempotency_key", "provider_ref"}:
            return v[:120] or None
        if key == "description":
            return v[:255] or None
        return v

    # ---------- Helpers ----------
    def set_amounts(self, *, gross: Decimal | int | float, fee: Decimal | int | float = 0, tax: Decimal | int | float = 0) -> None:
        self.amount_gross = _money(gross)
        self.fee = _money(fee)
        self.tax = _money(tax)
        if self.fee < 0 or self.tax < 0:
            raise ValueError("fee/tax must be non-negative.")

    def set_fee_tax(self, *, fee: Decimal | int | float = 0, tax: Decimal | int | float = 0) -> None:
        self.fee = _money(fee)
        self.tax = _money(tax)
        if self.fee < 0 or self.tax < 0:
            raise ValueError("fee/tax must be non-negative.")

    def mark_pending(self) -> None:
        self.status = BillingStatus.pending

    def mark_success(self) -> None:
        self.status = BillingStatus.success

    def mark_failed(self) -> None:
        self.status = BillingStatus.failed

    def mark_refund(self, gross: Decimal | int | float) -> None:
        """Weka refund kama negative gross; status → refunded."""
        self.amount_gross = -abs(_money(gross))
        self.status = BillingStatus.refunded

    def ensure_idempotency(self, key: Optional[str]) -> None:
        """Normalize/set idempotency_key (truncate 120)."""
        self.idempotency_key = (key or "").strip()[:120] or None

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<BillingLog id={self.id} user={self.user_id} type={self.type} "
            f"status={self.status} gross={self.amount_gross} net={self.amount_net} {self.currency}>"
        )

    # ---------- Constraints & Indexes ----------
    __table_args__ = (
        CheckConstraint("length(trim(currency)) >= 2", name="ck_billing_currency_len"),
        CheckConstraint("fee >= 0",  name="ck_billing_fee_nonneg"),
        CheckConstraint("tax >= 0",  name="ck_billing_tax_nonneg"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_billing_idem_per_user"),
        Index("ix_billing_user_created", "user_id", "created_at"),
        Index("ix_billing_status_provider", "status", "provider"),
        Index("ix_billing_type_status", "type", "status"),
        Index("ix_billing_provider_ref", "provider", "provider_ref"),
    )


# -------------------- Normalizers (events) --------------------
@listens_for(BillingLog, "before_insert")
def _bill_before_insert(_m, _c, t: BillingLog) -> None:
    # Safisha/quantize
    if t.currency:
        t.currency = t.currency.strip().upper()[:8]
    if t.reference:
        t.reference = t.reference.strip()[:120]
    if t.idempotency_key:
        t.idempotency_key = t.idempotency_key.strip()[:120]
    if t.provider_ref:
        t.provider_ref = t.provider_ref.strip()[:120]
    if t.description:
        t.description = t.description.strip()[:255]

    t.amount_gross = _money(t.amount_gross or 0)
    t.fee = _money(t.fee or 0)
    t.tax = _money(t.tax or 0)

    if t.fee < 0 or t.tax < 0:
        raise ValueError("fee/tax must be non-negative.")
    # Net inaweza kuwa negative (refund), sawa.


@listens_for(BillingLog, "before_update")
def _bill_before_update(_m, _c, t: BillingLog) -> None:
    if t.currency:
        t.currency = t.currency.strip().upper()[:8]
    if t.reference:
        t.reference = t.reference.strip()[:120]
    if t.idempotency_key:
        t.idempotency_key = t.idempotency_key.strip()[:120]
    if t.provider_ref:
        t.provider_ref = t.provider_ref.strip()[:120]
    if t.description:
        t.description = t.description.strip()[:255]

    t.amount_gross = _money(t.amount_gross or 0)
    t.fee = _money(t.fee or 0)
    t.tax = _money(t.tax or 0)

    if t.fee < 0 or t.tax < 0:
        raise ValueError("fee/tax must be non-negative.")
