# backend/models/order_item.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING, Dict, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

if TYPE_CHECKING:
    from .order import Order
    from .product import Product

_CCY_LEN = 3
_DEC_PLACES = Decimal("0.01")  # 2dp

def _q(x: Decimal | int | float | str | None) -> Decimal:
    if x is None:
        return Decimal("0.00")
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    return d.quantize(_DEC_PLACES, rounding=ROUND_HALF_UP)


class OrderItem(Base):
    """
    OrderItem — snapshot ya bidhaa ndani ya order wakati wa ununuzi.
    - Huhifadhi jina/sku/currency/bei kwa wakati huo.
    - Decimal (18,2) kwa usahihi wa fedha.
    - Auto-recalc ya subtotal & total inapobadilika qty/bei/discount/tax.
    - JSON meta ni mutable (tracked) kwa mabadiliko ya papo hapo.
    """
    __tablename__ = "order_items"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Links
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # Snapshots
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    sku: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    currency: Mapped[str] = mapped_column(String(_CCY_LEN), server_default=text("'TZS'"), nullable=False, index=True)

    # Monetary / qty (all stored, recalculated by hooks/validators)
    quantity:       Mapped[int]     = mapped_column(Integer, server_default=text("1"), nullable=False)
    unit_price:     Mapped[Decimal] = mapped_column(DECIMAL_TYPE, server_default=text("0"), nullable=False)
    discount_total: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, server_default=text("0"), nullable=False)
    tax_total:      Mapped[Decimal] = mapped_column(DECIMAL_TYPE, server_default=text("0"), nullable=False)

    # Computed & stored (fast reads / reports)
    line_subtotal:  Mapped[Decimal] = mapped_column(DECIMAL_TYPE, server_default=text("0"), nullable=False)
    line_total:     Mapped[Decimal] = mapped_column(DECIMAL_TYPE, server_default=text("0"), nullable=False)

    # Extra snapshot data (variant/options)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    # Relationships
    order: Mapped["Order"] = relationship(
        "Order", back_populates="items", passive_deletes=True, lazy="selectin"
    )
    product: Mapped[Optional["Product"]] = relationship("Product", lazy="selectin")

    # ---------- Helpers & validation ----------
    def recalc(self) -> None:
        """Recompute subtotal & total with safe clamping and rounding."""
        qty  = max(1, int(self.quantity or 1))
        up   = _q(self.unit_price)
        disc = _q(self.discount_total)
        tax  = _q(self.tax_total)

        sub = _q(up * qty)
        total = _q(sub + tax - disc)
        if total < Decimal("0.00"):
            total = Decimal("0.00")

        self.line_subtotal = sub
        self.line_total = total

    @validates("quantity", "unit_price", "discount_total", "tax_total")
    def _validate_amounts(self, key, value):
        if key == "quantity":
            v = int(value or 1)
            if v < 1:
                v = 1
            setattr(self, key, v)
        else:
            v = _q(value)
            if v < 0:
                v = Decimal("0.00")
            setattr(self, key, v)
        # Recalc immediately (in-memory consistency)
        try:
            self.recalc()
        except Exception:
            pass
        return getattr(self, key)

    @validates("currency")
    def _validate_currency(self, _k: str, v: str) -> str:
        s = (v or "TZS").strip().upper()
        if len(s) != _CCY_LEN:
            s = "TZS"
        return s

    @validates("sku", "name")
    def _sanitize_text(self, key, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None if key == "sku" else "Unnamed"
        v = value.strip()
        if not v:
            return None if key == "sku" else "Unnamed"
        return v

    # Quick flags / previews
    @hybrid_property
    def has_discount(self) -> bool:
        return _q(self.discount_total) > Decimal("0.00")

    @hybrid_property
    def preview(self) -> str:
        return f"{self.name} ×{self.quantity} @ {self.unit_price} {self.currency}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OrderItem id={self.id} order={self.order_id} sku={self.sku} qty={self.quantity} total={self.line_total} {self.currency}>"

    __table_args__ = (
        # Indexing for common lookups and report queries
        Index("ix_orderitem_order", "order_id"),
        Index("ix_orderitem_product", "product_id"),
        Index("ix_orderitem_order_id_id", "order_id", "id"),
        # (Optional) prevent duplicate SKU within the same order (nullable SKU allowed cross-DB)
        UniqueConstraint("order_id", "sku", name="uq_orderitem_order_sku"),
        # Data guards
        CheckConstraint("quantity >= 1", name="ck_orderitem_qty_min1"),
        CheckConstraint(f"length(currency) = {_CCY_LEN}", name="ck_orderitem_currency_iso3"),
        CheckConstraint(
            "unit_price >= 0 AND discount_total >= 0 AND tax_total >= 0",
            name="ck_orderitem_amounts_nonneg",
        ),
        CheckConstraint("line_subtotal >= 0 AND line_total >= 0", name="ck_orderitem_line_nonneg"),
        {"extend_existing": True},
    )


# ---------- Normalization & hooks ----------
@listens_for(OrderItem, "before_insert")
def _oi_before_insert(_m, _c, t: OrderItem) -> None:  # pragma: no cover
    # Sarafu isilingane na Order (ikijulikana) ili kuepuka mchanganyiko wa FX
    try:
        if t.order and t.order.currency and t.currency != t.order.currency:
            t.currency = t.order.currency
    except Exception:
        pass
    # Recalc once before writing
    t.recalc()


@listens_for(OrderItem, "before_update")
def _oi_before_update(_m, _c, t: OrderItem) -> None:  # pragma: no cover
    # Keep currency aligned with parent order if set
    try:
        if t.order and t.order.currency and t.currency != t.order.currency:
            t.currency = t.order.currency
    except Exception:
        pass
    t.recalc()
