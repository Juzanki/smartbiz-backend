# backend/models/payment.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import enum
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING, Dict, Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
    func,
)
from sqlalchemy.event import listens_for
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    # from .order import Order

# ---------------- Enums ----------------
class PaymentMethod(str, enum.Enum):
    mobile_money = "mobile_money"  # M-Pesa/TigoPesa/AirtelMoney
    card         = "card"
    bank         = "bank"
    wallet       = "wallet"        # in-app wallet
    cash         = "cash"
    other        = "other"

class PaymentProvider(str, enum.Enum):
    mpesa       = "mpesa"
    airtelmoney = "airtelmoney"
    tigopesa    = "tigopesa"
    paypal      = "paypal"
    stripe      = "stripe"
    pesapal     = "pesapal"
    paystack    = "paystack"
    flutterwave = "flutterwave"
    bank        = "bank"
    cash        = "cash"
    test        = "test"
    other       = "other"

class PaymentStatus(str, enum.Enum):
    pending    = "pending"     # created, not authorized yet / awaiting user action
    processing = "processing"  # in-flight with PSP
    succeeded  = "succeeded"   # completed with capture amount > 0 OR wallet/cash settled
    failed     = "failed"
    canceled   = "canceled"
    refunded   = "refunded"
    partially_refunded = "partially_refunded"

class Environment(str, enum.Enum):
    live    = "live"
    sandbox = "sandbox"

# ---------------- Helpers ----------------
_DEC2 = Decimal("0.01")
_phone_clean = re.compile(r"[^\d+]")

def _q(x: Decimal | int | float | str | None) -> Decimal:
    if x is None:
        return Decimal("0.00")
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    return d.quantize(_DEC2, rounding=ROUND_HALF_UP)

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def normalize_phone_e164(phone: Optional[str]) -> Optional[str]:
    """Normalize to a simple E.164-like form without inferring country code."""
    if not phone:
        return None
    p = phone.strip()
    if p.startswith("+"):
        p = "+" + re.sub(r"\D", "", p[1:])
    else:
        p = re.sub(r"\D", "", p)
    return p or None

# ---------------- Model ----------------
class Payment(Base):
    """
    Rekodi ya malipo (portable, production-grade):
      - UUID PK
      - method/provider/status/environment
      - NUMERIC(…): authorized/captured/refunded/fees + net
      - E.164 phone normalization (MM)
      - Idempotency/dedupe + provider refs + collapse_key
      - Reconciliation: settlement flags, payout batch, risk flags
      - JSON payloads (mutable) & lifecycle helpers
    """
    __tablename__ = "payments"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("reference", name="uq_payment_reference"),
        UniqueConstraint("idempotency_key", name="uq_payment_idem"),
        UniqueConstraint("provider_txn_id", name="uq_payment_provider_txn"),
        Index("ix_payment_user_created", "user_id", "created_at"),
        Index("ix_payment_status_time", "status", "created_at"),
        Index("ix_payment_provider_method", "provider", "method"),
        Index("ix_payment_phone", "payer_phone_e164"),
        Index("ix_payment_collapse", "collapse_key"),
        # Lifecyle invariants (light, cross-DB friendly)
        CheckConstraint("(status <> 'succeeded') OR (succeeded_at IS NOT NULL)", name="ck_payment_succeeded_ts"),
        CheckConstraint("(status <> 'refunded') OR (refunded_at IS NOT NULL)", name="ck_payment_refunded_ts"),
        CheckConstraint("(status <> 'partially_refunded') OR (refunded_at IS NOT NULL)", name="ck_payment_pr_ts"),
        CheckConstraint("(status <> 'canceled') OR (canceled_at IS NOT NULL)", name="ck_payment_canceled_ts"),
        # Money guards
        CheckConstraint("length(currency) = 3", name="ck_payment_currency_iso3"),
        CheckConstraint(
            "amount_authorized >= 0 AND amount_captured >= 0 AND amount_refunded >= 0 AND fee_total >= 0",
            name="ck_payment_amounts_nonneg",
        ),
        {"extend_existing": True},
    )

    # PK: UUID string
    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True, default=lambda: str(uuid4()))

    # Recipient (owner) of the payment
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    user: Mapped["User"] = relationship(
        "User",
        back_populates="payments",
        passive_deletes=True,
        lazy="selectin",
    )

    # (Optional) link to orders
    # order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), index=True)
    # order: Mapped[Optional["Order"]] = relationship("Order", lazy="selectin")

    # Classification
    method: Mapped[PaymentMethod] = mapped_column(
        SQLEnum(PaymentMethod, name="payment_method", native_enum=False, validate_strings=True),
        default=PaymentMethod.mobile_money, nullable=False, index=True,
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        SQLEnum(PaymentProvider, name="payment_provider", native_enum=False, validate_strings=True),
        default=PaymentProvider.mpesa, nullable=False, index=True,
    )
    status: Mapped[PaymentStatus] = mapped_column(
        SQLEnum(PaymentStatus, name="payment_status", native_enum=False, validate_strings=True),
        default=PaymentStatus.pending, nullable=False, index=True,
    )
    environment: Mapped[Environment] = mapped_column(
        SQLEnum(Environment, name="payment_env", native_enum=False, validate_strings=True),
        default=Environment.live, nullable=False, index=True,
    )

    # Currency & amounts
    currency:        Mapped[str]     = mapped_column(String(3), default="TZS", nullable=False, index=True)
    amount_authorized: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    amount_captured:   Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    amount_refunded:   Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    fee_total:         Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # Phone & payer info
    payer_phone:      Mapped[Optional[str]] = mapped_column(String(32))   # raw
    payer_phone_e164: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    payer_name:       Mapped[Optional[str]] = mapped_column(String(160))
    payer_email:      Mapped[Optional[str]] = mapped_column(String(160), index=True)

    # References / dedupe
    reference:       Mapped[str] = mapped_column(String(100), nullable=False, index=True)   # merchant reference
    provider_txn_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)         # PSP txn id / receipt no
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    request_id:      Mapped[Optional[str]] = mapped_column(String(64), index=True)
    collapse_key:    Mapped[Optional[str]] = mapped_column(String(120), index=True, doc="Coalesce duplicate PSP callbacks")

    # Provider payloads / metadata (mutable JSON for in-place changes)
    provider_response: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    meta:              Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    statement_descriptor: Mapped[Optional[str]] = mapped_column(String(64))

    # Risk & reconciliation
    risk_score:  Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))  # 0..100
    risk_flags:  Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    settlement_date:  Mapped[Optional[dt.date]] = mapped_column()
    payout_batch_id:  Mapped[Optional[str]] = mapped_column(String(120), index=True)
    settled:          Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    authorized_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    captured_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    succeeded_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    failed_at:     Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    canceled_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    refunded_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    expires_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # ---------------- Hybrids ----------------
    @hybrid_property
    def gross_amount(self) -> Decimal:
        cap = self.amount_captured or Decimal("0")
        auth = self.amount_authorized or Decimal("0")
        return _q(cap if cap > 0 else auth)

    @hybrid_property
    def net_amount(self) -> Decimal:
        cap = _q(self.amount_captured)
        ref = _q(self.amount_refunded)
        fee = _q(self.fee_total)
        net = cap - ref - fee
        return _q(net if net > 0 else Decimal("0.00"))

    @hybrid_property
    def is_terminal(self) -> bool:
        return self.status in (
            PaymentStatus.succeeded,
            PaymentStatus.failed,
            PaymentStatus.canceled,
            PaymentStatus.refunded,
            PaymentStatus.partially_refunded,
        )

    @hybrid_property
    def is_refunded_full(self) -> bool:
        return _q(self.amount_refunded) >= _q(self.amount_captured) and \
               self.status in (PaymentStatus.refunded, PaymentStatus.partially_refunded)

    @hybrid_property
    def is_refunded_partial(self) -> bool:
        return Decimal("0.00") < _q(self.amount_refunded) < _q(self.amount_captured)

    # ---------------- Validators ----------------
    @validates("currency")
    def _v_currency(self, _key: str, value: str) -> str:
        v = (value or "TZS").strip().upper()
        if len(v) != 3:
            raise ValueError("currency must be a 3-letter ISO code")
        return v

    @validates("reference", "provider_txn_id", "idempotency_key", "request_id",
               "collapse_key", "payer_name", "payer_email", "statement_descriptor",
               "payout_batch_id")
    def _v_trim(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    @validates("amount_authorized", "amount_captured", "amount_refunded", "fee_total")
    def _v_amounts(self, _k: str, v):
        d = _q(v)
        if d < 0:
            d = Decimal("0.00")
        return d

    @validates("payer_phone")
    def _v_payer_phone(self, _key: str, value: Optional[str]) -> Optional[str]:
        return (value or None).strip() if value else None

    # ---------------- Helpers (domain) ----------------
    def normalize_phone(self) -> None:
        self.payer_phone_e164 = normalize_phone_e164(self.payer_phone)

    def mark_processing(self) -> None:
        self.status = PaymentStatus.processing

    def authorize(self, amount: Decimal | int | float | str) -> None:
        amt = _q(amount)
        self.amount_authorized = max(Decimal("0.00"), amt)
        self.authorized_at = _utcnow()
        self.status = PaymentStatus.processing

    def capture(self, amount: Optional[Decimal | int | float | str] = None, *, allow_over_capture: bool = False) -> None:
        """Capture (single-shot). If amount=None, capture up to authorized."""
        target = _q(amount if amount is not None else (self.amount_authorized or 0))
        if (not allow_over_capture) and target > _q(self.amount_authorized):
            target = _q(self.amount_authorized)
        # prevent double-growing beyond target on repeated calls:
        self.amount_captured = target
        self.captured_at = _utcnow()
        self.status = PaymentStatus.processing

    def capture_incremental(self, amount: Decimal | int | float | str, *, allow_over_capture: bool = False) -> None:
        """Add to previously captured amount (for PSPs supporting multiple captures)."""
        inc = _q(amount)
        new_total = _q(self.amount_captured) + inc
        if (not allow_over_capture) and new_total > _q(self.amount_authorized):
            new_total = _q(self.amount_authorized)
        self.amount_captured = new_total
        self.captured_at = _utcnow()
        self.status = PaymentStatus.processing

    def succeed(self) -> None:
        self.status = PaymentStatus.succeeded
        self.succeeded_at = _utcnow()

    def fail(self) -> None:
        self.status = PaymentStatus.failed
        self.failed_at = _utcnow()

    def cancel(self) -> None:
        self.status = PaymentStatus.canceled
        self.canceled_at = _utcnow()

    def add_fee(self, amount: Decimal | int | float | str) -> None:
        amt = _q(amount)
        self.fee_total = _q((self.fee_total or Decimal("0.00")) + max(Decimal("0.00"), amt))

    def refund(self, amount: Optional[Decimal | int | float | str] = None) -> None:
        """Partial/full refund. Caps at amount_captured."""
        if amount is not None:
            amt = _q(amount)
        else:
            amt = _q(self.amount_captured) - _q(self.amount_refunded)  # remaining to full
        if amt <= 0:
            return
        new_total = _q(self.amount_refunded) + amt
        cap = _q(self.amount_captured)
        if new_total > cap:
            new_total = cap
        self.amount_refunded = new_total
        self.refunded_at = _utcnow()
        self.status = PaymentStatus.refunded if self.amount_refunded >= cap else PaymentStatus.partially_refunded

    def set_provider_payload(self, *, response: Optional[Dict[str, Any]] = None, meta: Optional[Dict[str, Any]] = None) -> None:
        if response:
            self.provider_response = {**(self.provider_response or {}), **response}
        if meta:
            self.meta = {**(self.meta or {}), **meta}

    def mark_settled(self, *, on: Optional[dt.date] = None, payout_batch_id: Optional[str] = None) -> None:
        self.settled = True
        self.settlement_date = on or dt.date.today()
        if payout_batch_id:
            self.payout_batch_id = payout_batch_id

    # ---------------- Reprs ----------------
    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Payment id={self.id} user={self.user_id} status={self.status} "
            f"gross={self.gross_amount} net={self.net_amount} {self.currency} provider={self.provider}>"
        )

# ---------------- Listeners: normalize data before persist ----------------
@listens_for(Payment, "before_insert")
def _payment_before_insert(_m, _c, t: Payment) -> None:  # pragma: no cover
    if t.currency:
        t.currency = t.currency.strip().upper()
    if t.reference:
        t.reference = t.reference.strip()
    t.amount_authorized = _q(t.amount_authorized)
    t.amount_captured   = _q(t.amount_captured)
    t.amount_refunded   = _q(t.amount_refunded)
    t.fee_total         = _q(t.fee_total)
    t.normalize_phone()

@listens_for(Payment, "before_update")
def _payment_before_update(_m, _c, t: Payment) -> None:  # pragma: no cover
    if t.currency:
        t.currency = t.currency.strip().upper()
    if t.reference:
        t.reference = t.reference.strip()
    t.amount_authorized = _q(t.amount_authorized)
    t.amount_captured   = _q(t.amount_captured)
    t.amount_refunded   = _q(t.amount_refunded)
    t.fee_total         = _q(t.fee_total)
    t.normalize_phone()
