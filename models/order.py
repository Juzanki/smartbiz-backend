# backend/models/order.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING, Dict, Any, List

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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    from .product import Product
    from .order_item import OrderItem


# ---------------- Enums ----------------
class OrderStatus(str, enum.Enum):
    pending   = "pending"
    confirmed = "confirmed"
    paid      = "paid"
    fulfilled = "fulfilled"
    canceled  = "canceled"
    refunded  = "refunded"
    failed    = "failed"


class PaymentStatus(str, enum.Enum):
    unpaid   = "unpaid"
    partial  = "partial"
    paid     = "paid"
    overpaid = "overpaid"


class FulfillmentStatus(str, enum.Enum):
    unfulfilled = "unfulfilled"
    partial     = "partial"
    fulfilled   = "fulfilled"
    returned    = "returned"


class ShippingStatus(str, enum.Enum):
    not_required = "not_required"
    pending      = "pending"
    in_transit   = "in_transit"
    delivered    = "delivered"
    failed       = "failed"
    returned     = "returned"


class PaymentMethod(str, enum.Enum):
    unknown   = "unknown"
    card      = "card"
    wallet    = "wallet"
    bank      = "bank"
    cash      = "cash"
    mobile    = "mobile"
    voucher   = "voucher"
    other     = "other"


# ---------------- Model ----------------
class Order(Base):
    """Order model (portable SQLite/MySQL/Postgres) with hardened lifecycle & totals."""
    __tablename__ = "orders"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("reference_code", name="uq_order_reference"),
        UniqueConstraint("idempotency_key", name="uq_order_idem"),
        Index("ix_order_user_created", "user_id", "created_at"),
        Index("ix_order_status_time", "status", "created_at"),
        Index("ix_order_payment_status", "payment_status"),
        Index("ix_order_ref_ext", "reference_code", "external_ref"),
        Index("ix_order_shipping_state", "shipping_status", "fulfillment_status"),
        # Money guards
        CheckConstraint("length(trim(currency)) = 3", name="ck_order_currency_iso3"),
        CheckConstraint(
            "subtotal >= 0 AND tax_total >= 0 AND shipping_total >= 0 AND discount_total >= 0",
            name="ck_order_amounts_nonneg",
        ),
        CheckConstraint(
            "grand_total >= 0 AND paid_total >= 0 AND refunded_total >= 0",
            name="ck_order_totals_nonneg",
        ),
        # Lifecycle (light, cross-DB)
        CheckConstraint("(status <> 'paid') OR (paid_at IS NOT NULL)", name="ck_order_paid_ts"),
        CheckConstraint("(status <> 'fulfilled') OR (fulfilled_at IS NOT NULL)", name="ck_order_fulfilled_ts"),
        CheckConstraint("(status <> 'canceled') OR (canceled_at IS NOT NULL)", name="ck_order_canceled_ts"),
        CheckConstraint("(status <> 'refunded') OR (refunded_at IS NOT NULL)", name="ck_order_refunded_ts"),
        {"extend_existing": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Owner
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user: Mapped["User"] = relationship(
        "User",
        back_populates="orders",
        foreign_keys=[user_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # (Hiari) single-product compatibility
    product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), index=True, default=None
    )
    product: Mapped[Optional["Product"]] = relationship(
        "Product",
        lazy="selectin",
        passive_deletes=True,
    )

    # Classification & refs
    status: Mapped[OrderStatus] = mapped_column(
        SQLEnum(OrderStatus, name="order_status", native_enum=False, validate_strings=True),
        default=OrderStatus.pending,
        nullable=False,
        index=True,
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        SQLEnum(PaymentStatus, name="order_payment_status", native_enum=False, validate_strings=True),
        default=PaymentStatus.unpaid,
        nullable=False,
        index=True,
    )
    fulfillment_status: Mapped[FulfillmentStatus] = mapped_column(
        SQLEnum(FulfillmentStatus, name="order_fulfillment_status", native_enum=False, validate_strings=True),
        default=FulfillmentStatus.unfulfilled,
        nullable=False,
        index=True,
    )
    shipping_status: Mapped[ShippingStatus] = mapped_column(
        SQLEnum(ShippingStatus, name="order_shipping_status", native_enum=False, validate_strings=True),
        default=ShippingStatus.pending,
        nullable=False,
        index=True,
    )

    reference_code:  Mapped[Optional[str]] = mapped_column(String(32), index=True)
    external_ref:    Mapped[Optional[str]] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), index=True)

    # Currency & money
    currency:       Mapped[str]     = mapped_column(String(3), default="TZS", nullable=False, index=True)
    subtotal:       Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    tax_total:      Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    discount_total: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    shipping_total: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    grand_total:    Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    paid_total:     Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    refunded_total: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # Payment/fulfillment metadata
    payment_method:  Mapped[PaymentMethod] = mapped_column(
        SQLEnum(PaymentMethod, name="order_payment_method", native_enum=False, validate_strings=True),
        default=PaymentMethod.unknown, nullable=False, index=True
    )
    payment_collapse_key: Mapped[Optional[str]] = mapped_column(
        String(120), index=True, doc="Coalesce duplicate payment webhooks/attempts"
    )
    provider_payment_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    provider_ref:        Mapped[Optional[str]] = mapped_column(String(160), index=True)

    # Shipping
    shipping_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    shipping_method:   Mapped[Optional[str]] = mapped_column(String(80))
    shipping_tracking: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    shipping_carrier:  Mapped[Optional[str]] = mapped_column(String(80))

    # Snapshots/meta (mutable JSON so in-place edits get tracked)
    shipping_address: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    billing_address:  Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    contact:          Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))  # {"email":..., "phone":...}
    risk_flags:       Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))  # {"ip_reputation":"high", ...}
    meta:             Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Times
    created_at:  Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at:  Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    confirmed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    paid_at:       Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    fulfilled_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    canceled_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    refunded_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Multi-items
    items: Mapped[List["OrderItem"]] = relationship(
        "OrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # ---------- Internal rounding helper ----------
    @staticmethod
    def _q(x: Decimal | int | float | str) -> Decimal:
        d = x if isinstance(x, Decimal) else Decimal(str(x))
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # ---------- Hybrids ----------
    @hybrid_property
    def net_paid(self) -> Decimal:
        return self._q((self.paid_total or Decimal("0")) - (self.refunded_total or Decimal("0")))

    @hybrid_property
    def balance_due(self) -> Decimal:
        due = self._q((self.grand_total or Decimal("0")) - self.net_paid)
        return due if due > 0 else Decimal("0.00")

    @hybrid_property
    def is_fully_paid(self) -> bool:
        return self.net_paid >= self._q(self.grand_total or Decimal("0"))

    @hybrid_property
    def is_cancelable(self) -> bool:
        return self.status in {OrderStatus.pending, OrderStatus.confirmed} and not self.fulfilled_at

    @hybrid_property
    def is_refundable(self) -> bool:
        return self.paid_at is not None and self.net_paid > Decimal("0")

    # ---------- Totals ----------
    def recalc_totals(self) -> None:
        """Recompute totals kutoka items; husasisha payment_status pia (rounded)."""
        if self.items:
            self.subtotal = self._q(sum((i.line_subtotal or Decimal("0")) for i in self.items))
            self.tax_total = self._q(sum((i.tax_total or Decimal("0")) for i in self.items))
            self.discount_total = self._q(sum((i.discount_total or Decimal("0")) for i in self.items))
        else:
            self.subtotal = self._q(self.subtotal or 0)
            self.tax_total = self._q(self.tax_total or 0)
            self.discount_total = self._q(self.discount_total or 0)

        self.shipping_total = self._q(self.shipping_total or 0)
        self.paid_total = self._q(self.paid_total or 0)
        self.refunded_total = self._q(self.refunded_total or 0)

        self.grand_total = max(
            Decimal("0.00"),
            self._q(self.subtotal + self.tax_total + self.shipping_total - self.discount_total),
        )

        if self.net_paid <= 0:
            self.payment_status = PaymentStatus.unpaid
        elif self.net_paid < self.grand_total:
            self.payment_status = PaymentStatus.partial
        elif self.net_paid == self.grand_total:
            self.payment_status = PaymentStatus.paid
        else:
            self.payment_status = PaymentStatus.overpaid

    # ---------- Domain helpers ----------
    def apply_payment(self, amount: Decimal | int | float | str) -> None:
        amt = self._q(amount)
        self.paid_total = self._q((self.paid_total or Decimal("0")) + max(Decimal("0.00"), amt))
        if self.is_fully_paid and not self.paid_at:
            self.paid_at = dt.datetime.now(dt.timezone.utc)
            self.status = OrderStatus.paid
        self.recalc_totals()

    def apply_refund(self, amount: Decimal | int | float | str) -> None:
        amt = self._q(amount)
        self.refunded_total = self._q((self.refunded_total or Decimal("0")) + max(Decimal("0.00"), amt))
        if self.refunded_total >= (self.paid_total or Decimal("0")) and not self.refunded_at:
            self.refunded_at = dt.datetime.now(dt.timezone.utc)
            self.status = OrderStatus.refunded
        self.recalc_totals()

    def confirm(self) -> None:
        self.status = OrderStatus.confirmed
        self.confirmed_at = self.confirmed_at or dt.datetime.now(dt.timezone.utc)

    def fulfill(self) -> None:
        self.status = OrderStatus.fulfilled
        self.fulfillment_status = FulfillmentStatus.fulfilled
        self.shipping_status = ShippingStatus.delivered if not self.shipping_required else self.shipping_status
        self.fulfilled_at = self.fulfilled_at or dt.datetime.now(dt.timezone.utc)

    def cancel(self) -> None:
        self.status = OrderStatus.canceled
        self.canceled_at = self.canceled_at or dt.datetime.now(dt.timezone.utc)

    # Shipping helpers
    def mark_shipped(self, *, tracking: Optional[str] = None, carrier: Optional[str] = None) -> None:
        if tracking:
            self.shipping_tracking = tracking.strip() or None
        if carrier:
            self.shipping_carrier = carrier.strip() or None
        self.shipping_status = ShippingStatus.in_transit

    def mark_delivered(self) -> None:
        self.shipping_status = ShippingStatus.delivered
        if self.fulfillment_status != FulfillmentStatus.fulfilled:
            self.fulfillment_status = FulfillmentStatus.partial

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Order id={self.id} user={self.user_id} status={self.status} total={self.grand_total} {self.currency}>"

    # ---------- Validations ----------
    @validates("currency")
    def _validate_currency(self, _k: str, v: str) -> str:
        v = (v or "").strip().upper()
        if len(v) != 3:
            raise ValueError("currency must be a 3-letter ISO code (e.g., TZS, USD)")
        return v

    @validates("reference_code", "external_ref", "idempotency_key",
               "payment_collapse_key", "provider_payment_id", "provider_ref",
               "shipping_method", "shipping_tracking", "shipping_carrier")
    def _trim_short_texts(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None


# ---------------- Normalization & hooks ----------------
@listens_for(Order, "before_insert")
def _order_before_insert(_m, _c, t: Order) -> None:  # pragma: no cover
    if t.currency:
        t.currency = t.currency.strip().upper()
    # Normalize and compute totals once
    t.recalc_totals()
    # If shipping not required → normalize status
    if not t.shipping_required:
        t.shipping_status = ShippingStatus.not_required


@listens_for(Order, "before_update")
def _order_before_update(_m, _c, t: Order) -> None:  # pragma: no cover
    if t.currency:
        t.currency = t.currency.strip().upper()
    t.recalc_totals()
    if not t.shipping_required:
        t.shipping_status = ShippingStatus.not_required


# Optional: keep totals in sync when items list changes in-session
from sqlalchemy.orm import attributes

@listens_for(Order.items, "append")
def _order_items_append(order: Order, item: "OrderItem", _i) -> None:
    # Trigger recalc when session flushes
    if not attributes.instance_state(order).key:  # unsaved ok
        return
    order.recalc_totals()

@listens_for(Order.items, "remove")
def _order_items_remove(order: Order, item: "OrderItem", _i) -> None:
    if not attributes.instance_state(order).key:
        return
    order.recalc_totals()
