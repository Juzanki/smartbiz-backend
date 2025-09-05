# backend/models/referral_log.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from decimal import Decimal
from typing import Optional, TYPE_CHECKING, Dict, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
    func,
    JSON as SA_JSON,
    Numeric as SA_NUMERIC,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# ---------- Portable types ----------
try:
    from sqlalchemy.dialects.postgresql import NUMERIC as PG_NUMERIC  # type: ignore
    DECIMAL_TYPE = PG_NUMERIC(18, 2)
    RATE_TYPE    = PG_NUMERIC(10, 4)
except Exception:  # pragma: no cover
    DECIMAL_TYPE = SA_NUMERIC(18, 2)
    RATE_TYPE    = SA_NUMERIC(10, 4)

if TYPE_CHECKING:
    from .user import User
    from .campaign import Campaign
    from .order import Order
    from .payment import Payment
    from .product import Product

# ---------- Enums ----------
class ReferralStatus(str, enum.Enum):
    pending   = "pending"
    clicked   = "clicked"
    signed_up = "signed_up"
    qualified = "qualified"
    approved  = "approved"
    paid      = "paid"
    rejected  = "rejected"
    canceled  = "canceled"
    expired   = "expired"

class ReferralChannel(str, enum.Enum):
    link   = "link"
    code   = "code"
    qr     = "qr"
    social = "social"
    email  = "email"
    sms    = "sms"
    other  = "other"

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class ReferralLog(Base):
    __tablename__ = "referral_logs"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Parties
    referrer_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    referred_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    referrer: Mapped["User"] = relationship(
        "User",
        foreign_keys=[referrer_id],
        back_populates="referrals_made",
        passive_deletes=True,
        lazy="selectin",
    )
    referred_user: Mapped["User"] = relationship(
        "User",
        foreign_keys=[referred_user_id],
        back_populates="referrals_received",
        passive_deletes=True,
        lazy="selectin",
    )

    # Optional links
    campaign_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )
    campaign: Mapped[Optional["Campaign"]] = relationship("Campaign", lazy="selectin")

    product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    product: Mapped[Optional["Product"]] = relationship("Product", lazy="selectin")
    product_name: Mapped[Optional[str]] = mapped_column(String(120))

    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )
    order: Mapped[Optional["Order"]] = relationship("Order", lazy="selectin")

    # NOTE: we use str to align with Payment UUID PK; change to int if your Payment uses int
    payment_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL"), index=True
    )
    payment: Mapped[Optional["Payment"]] = relationship("Payment", lazy="selectin")

    # Channel & UTM
    channel: Mapped[ReferralChannel] = mapped_column(
        SQLEnum(ReferralChannel, name="referral_channel", native_enum=False, validate_strings=True),
        default=ReferralChannel.link,
        nullable=False,
        index=True,
    )
    referral_code: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    utm_source:   Mapped[Optional[str]] = mapped_column(String(80))
    utm_medium:   Mapped[Optional[str]] = mapped_column(String(80))
    utm_campaign: Mapped[Optional[str]] = mapped_column(String(80))
    utm_content:  Mapped[Optional[str]] = mapped_column(String(80))
    landing_url:  Mapped[Optional[str]] = mapped_column(String(512))
    ip_address:   Mapped[Optional[str]] = mapped_column(String(64))
    user_agent:   Mapped[Optional[str]] = mapped_column(String(400))
    country:      Mapped[Optional[str]] = mapped_column(String(2), index=True)

    # Amounts
    currency:          Mapped[Optional[str]] = mapped_column(String(3), index=True)
    purchase_amount:   Mapped[Decimal]       = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    commission_rate:   Mapped[Decimal]       = mapped_column(RATE_TYPE,    nullable=False, server_default=text("0"))
    commission_amount: Mapped[Decimal]       = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    payout_fee:        Mapped[Decimal]       = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))  # optional PSP/processing fee

    status: Mapped[ReferralStatus] = mapped_column(
        SQLEnum(ReferralStatus, name="referral_status", native_enum=False, validate_strings=True),
        default=ReferralStatus.pending,
        nullable=False,
        index=True,
    )
    reason: Mapped[Optional[str]] = mapped_column(Text)
    suspected_fraud: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    external_ref:    Mapped[Optional[str]] = mapped_column(String(160), index=True)

    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Times
    created_at:   Mapped[dt.datetime]           = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    clicked_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    signed_up_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    qualified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    approved_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    paid_at:      Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    rejected_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    canceled_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    expired_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # ---------- Hybrids ----------
    @hybrid_property
    def commission_net(self) -> Decimal:
        """Commission baada ya kuondoa `payout_fee` (haiendi chini ya 0)."""
        net = (self.commission_amount or Decimal("0")) - (self.payout_fee or Decimal("0"))
        return net if net > 0 else Decimal("0")

    @hybrid_property
    def is_payable(self) -> bool:
        return self.status == ReferralStatus.approved and self.paid_at is None

    @hybrid_property
    def is_qualified(self) -> bool:
        return self.status in {ReferralStatus.qualified, ReferralStatus.approved, ReferralStatus.paid}

    @hybrid_property
    def is_terminal(self) -> bool:
        return self.status in {ReferralStatus.paid, ReferralStatus.rejected, ReferralStatus.canceled, ReferralStatus.expired}

    # ---------- Validators ----------
    @validates("currency")
    def _v_currency(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip().upper()
        return s[:3] or None

    @validates("country")
    def _v_country(self, _k: str, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        s = v.strip().upper()
        return s[:2] or None

    @validates("referral_code", "utm_source", "utm_medium", "utm_campaign", "utm_content",
               "landing_url", "ip_address", "user_agent", "idempotency_key", "external_ref", "reason")
    def _v_trim(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    # ---------- Business helpers ----------
    def set_purchase(self, *, amount: Decimal | int | float | str, currency: Optional[str] = None) -> None:
        """Sasisha manunuzi na ukubaliane na sarafu (ISO3)."""
        amt = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        self.purchase_amount = max(Decimal("0"), amt)
        if currency:
            self.currency = (currency or "").strip().upper()[:3] or None

    def set_rate(self, *, rate: Decimal | int | float | str) -> None:
        r = rate if isinstance(rate, Decimal) else Decimal(str(rate))
        # clamp 0..1
        if r < 0:
            r = Decimal("0")
        if r > 1:
            r = Decimal("1")
        self.commission_rate = r

    def compute_commission(self, *, rate: Decimal | int | float | None = None,
                           min_amount: Decimal | int | float | None = None,
                           max_amount: Decimal | int | float | None = None) -> None:
        """Hesabu commission = purchase * rate (uwekwe mipaka hiari)."""
        if rate is not None:
            self.set_rate(rate=rate)
        base = (self.purchase_amount or Decimal("0")) * (self.commission_rate or Decimal("0"))
        base = base.quantize(Decimal("0.01"))
        if min_amount is not None:
            mn = min_amount if isinstance(min_amount, Decimal) else Decimal(str(min_amount))
            if base < mn:
                base = mn
        if max_amount is not None:
            mx = max_amount if isinstance(max_amount, Decimal) else Decimal(str(max_amount))
            if base > mx:
                base = mx
        if base < 0:
            base = Decimal("0")
        self.commission_amount = base

    # ---- Lifecycle transitions ----
    def mark_clicked(self) -> None:
        self.status = ReferralStatus.clicked
        self.clicked_at = self.clicked_at or _utcnow()

    def mark_signed_up(self) -> None:
        self.status = ReferralStatus.signed_up
        self.signed_up_at = self.signed_up_at or _utcnow()

    def qualify(self) -> None:
        self.status = ReferralStatus.qualified
        self.qualified_at = self.qualified_at or _utcnow()

    def approve(self) -> None:
        self.status = ReferralStatus.approved
        self.approved_at = self.approved_at or _utcnow()

    def pay(self, *, payment_id: Optional[str] = None, fee: Decimal | int | float | None = None) -> None:
        if payment_id:
            self.payment_id = payment_id
        if fee is not None:
            f = fee if isinstance(fee, Decimal) else Decimal(str(fee))
            self.payout_fee = max(Decimal("0"), f)
        self.status = ReferralStatus.paid
        self.paid_at = _utcnow()

    def reject(self, *, reason: Optional[str] = None, fraud: Optional[bool] = None) -> None:
        self.status = ReferralStatus.rejected
        self.rejected_at = _utcnow()
        if reason:
            self.reason = reason.strip()
        if fraud is True:
            self.suspected_fraud = True

    def cancel(self, *, reason: Optional[str] = None) -> None:
        self.status = ReferralStatus.canceled
        self.canceled_at = _utcnow()
        if reason:
            self.reason = (self.reason + " | " if self.reason else "") + reason.strip()

    def expire(self) -> None:
        self.status = ReferralStatus.expired
        self.expired_at = _utcnow()

    # ---------- Repr ----------
    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ReferralLog id={self.id} referrer={self.referrer_id} "
            f"referred={self.referred_user_id} status={self.status} "
            f"commission={self.commission_amount} {self.currency}>"
        )

    # ---------- Table args ----------
    __table_args__ = (
        CheckConstraint("referrer_id <> referred_user_id", name="ck_referral_no_self"),
        CheckConstraint("purchase_amount >= 0 AND commission_amount >= 0 AND payout_fee >= 0", name="ck_ref_purchase_comm_nonneg"),
        CheckConstraint("commission_rate >= 0 AND commission_rate <= 1", name="ck_ref_commission_rate_0_1"),
        CheckConstraint("currency IS NULL OR length(currency) = 3", name="ck_ref_currency_iso3"),
        UniqueConstraint("idempotency_key", name="uq_referral_idem"),
        UniqueConstraint("campaign_id", "referrer_id", "referred_user_id", name="uq_referral_campaign_triplet"),
        Index("ix_referral_referrer_created", "referrer_id", "created_at"),
        Index("ix_referral_referred_created", "referred_user_id", "created_at"),
        Index("ix_referral_status_time", "status", "created_at"),
        Index("ix_referral_campaign", "campaign_id"),
        Index("ix_referral_order_payment", "order_id", "payment_id"),
        Index("ix_referral_channel_code", "channel", "referral_code"),
        Index("ix_referral_utm", "utm_source", "utm_medium", "utm_campaign"),
    )


# ---------- Normalization hooks ----------
@listens_for(ReferralLog, "before_insert")
def _ref_before_insert(_m, _c, t: ReferralLog) -> None:  # pragma: no cover
    if t.currency:
        t.currency = (t.currency or "").strip().upper()[:3] or None
    if t.country:
        t.country = (t.country or "").strip().upper()[:2] or None
    if t.referral_code:
        t.referral_code = t.referral_code.strip()
    # auto-compute commission if not set
    if (t.commission_amount or Decimal("0")) <= 0 and (t.purchase_amount or Decimal("0")) > 0:
        t.compute_commission()

@listens_for(ReferralLog, "before_update")
def _ref_before_update(_m, _c, t: ReferralLog) -> None:  # pragma: no cover
    if t.currency:
        t.currency = (t.currency or "").strip().upper()[:3] or None
    if t.country:
        t.country = (t.country or "").strip().upper()[:2] or None
    if t.referral_code:
        t.referral_code = t.referral_code.strip()
    # keep commission in sync if purchase/rate changed and commission not manually overridden
    # (override by setting meta={"commission_locked": true})
    locked = bool((t.meta or {}).get("commission_locked"))
    if not locked and (t.purchase_amount is not None or t.commission_rate is not None):
        t.compute_commission()
