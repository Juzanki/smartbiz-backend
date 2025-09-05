# backend/models/product.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import re
import datetime as dt
from decimal import Decimal
from typing import Optional, List, TYPE_CHECKING, Dict, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
    # hybrid_method for can_fulfill; hybrid_property for others
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    from .order import Order
    from .drone_mission import DroneMission

# ───────────── Utils ─────────────
_slug_re = re.compile(r"[^a-z0-9]+")

def _slugify(s: str, *, max_len: int = 120) -> str:
    s = (s or "").strip().lower()
    s = _slug_re.sub("-", s).strip("-")
    return s[:max_len] or "product"

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

# ───────────── Enums ─────────────
class ProductStatus(str, enum.Enum):
    draft = "draft"
    active = "active"
    archived = "archived"
    out_of_stock = "out_of_stock"

class TrackInventory(str, enum.Enum):
    none = "none"
    always = "always"
    when_active = "when_active"

class BackorderPolicy(str, enum.Enum):
    deny = "deny"
    allow = "allow"
    preorder = "preorder"


class Product(Base):
    """Bidhaa ya kuuza/kuonyesha; portable kwa SQLite/MySQL/Postgres."""
    __tablename__ = "products"
    __mapper_args__ = {"eager_defaults": True}

    # ── Identity ───────────────────────────────────────────────────────────────
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    owner_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner: Mapped[Optional["User"]] = relationship("User", lazy="selectin", foreign_keys=[owner_id])

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(String(140), index=True)
    sku: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    barcode: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)

    # ── Pricing ────────────────────────────────────────────────────────────────
    currency: Mapped[str] = mapped_column(String(3), default="TZS", nullable=False, index=True)
    price: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    compare_at_price: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    cost: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # ── Inventory ─────────────────────────────────────────────────────────────
    track_inventory: Mapped[TrackInventory] = mapped_column(
        SQLEnum(TrackInventory, name="product_track_inventory", native_enum=False, validate_strings=True),
        default=TrackInventory.always,
        nullable=False,
        index=True,
    )
    backorder_policy: Mapped[BackorderPolicy] = mapped_column(
        SQLEnum(BackorderPolicy, name="product_backorder_policy", native_enum=False, validate_strings=True),
        default=BackorderPolicy.deny,
        nullable=False,
        index=True,
    )
    stock_on_hand: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    stock_reserved: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    safety_stock: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    restock_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # ── Dimensions (SI) ───────────────────────────────────────────────────────
    weight_kg: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    width_cm: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    height_cm: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    length_cm: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # ── Media / Attributes (Mutable JSON variant) ─────────────────────────────
    images: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(
        as_mutable_json(JSON_VARIANT)
    )
    attributes: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        as_mutable_json(JSON_VARIANT)
    )
    tags: Mapped[Optional[List[str]]] = mapped_column(
        as_mutable_json(JSON_VARIANT)
    )
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        as_mutable_json(JSON_VARIANT)
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status: Mapped[ProductStatus] = mapped_column(
        SQLEnum(ProductStatus, name="product_status", native_enum=False, validate_strings=True),
        default=ProductStatus.active,
        nullable=False,
        index=True,
    )
    published_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # ── Relationships ─────────────────────────────────────────────────────────
    orders: Mapped[list["Order"]] = relationship(
        "Order",
        back_populates="product",
        lazy="selectin",
        passive_deletes=True,
    )

    missions: Mapped[list["DroneMission"]] = relationship(
        "DroneMission",
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # ── Hybrids ───────────────────────────────────────────────────────────────
    @hybrid_property
    def available_stock(self) -> int:
        on_hand = int(self.stock_on_hand or 0)
        reserved = int(self.stock_reserved or 0)
        safety = int(self.safety_stock or 0)
        return max(0, on_hand - reserved - safety)

    @hybrid_property
    def in_stock(self) -> bool:
        if self.track_inventory == TrackInventory.none:
            return True
        if self.available_stock > 0:
            return True
        return self.backorder_policy in (BackorderPolicy.allow, BackorderPolicy.preorder)

    @hybrid_property
    def margin(self) -> Decimal:
        p = self.price or Decimal("0")
        c = self.cost or Decimal("0")
        return max(Decimal("0"), p - c)

    @hybrid_method
    def can_fulfill(self, qty: int) -> bool:
        if self.track_inventory == TrackInventory.none:
            return True
        if qty <= 0:
            return True
        if self.available_stock >= qty:
            return True
        return self.backorder_policy in (BackorderPolicy.allow, BackorderPolicy.preorder)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def set_price(self, amount: Decimal | int | float | str) -> None:
        self.price = max(Decimal("0"), Decimal(str(amount)))

    def adjust_stock(self, delta: int) -> None:
        self.stock_on_hand = max(0, int(self.stock_on_hand or 0) + int(delta))

    def reserve(self, qty: int) -> bool:
        q = max(0, int(qty))
        if self.track_inventory == TrackInventory.none or q == 0:
            return True
        if self.available_stock >= q or self.backorder_policy != BackorderPolicy.deny:
            self.stock_reserved = max(0, int(self.stock_reserved or 0) + q)
            return True
        return False

    def commit(self, qty: int) -> None:
        q = max(0, int(qty))
        if q == 0:
            return
        self.stock_reserved = max(0, int(self.stock_reserved or 0) - q)
        if self.track_inventory != TrackInventory.none:
            self.stock_on_hand = max(0, int(self.stock_on_hand or 0) - q)

    def cancel_reservation(self, qty: int) -> None:
        q = max(0, int(qty))
        if q:
            self.stock_reserved = max(0, int(self.stock_reserved or 0) - q)

    def publish(self) -> None:
        self.status = ProductStatus.active
        self.published_at = self.published_at or _utcnow()

    def archive(self) -> None:
        self.status = ProductStatus.archived  # ← usiite kama function

    def soft_delete(self) -> None:
        self.deleted_at = _utcnow()
        self.status = ProductStatus.archived

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Product id={self.id} name={self.name!r} sku={self.sku} status={self.status}>"

    # ── Validations ───────────────────────────────────────────────────────────
    @validates("currency")
    def _validate_currency(self, _k: str, value: str) -> str:
        v = (value or "").strip().upper()
        if len(v) != 3:
            raise ValueError("currency must be a 3-letter ISO code (e.g., TZS, USD)")
        return v

    @validates("sku")
    def _validate_sku(self, _k: str, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        v = value.strip().upper()
        if len(v) > 64:
            raise ValueError("sku too long (max 64)")
        return v

    # ── Constraints & Indexes ────────────────────────────────────────────────
    __table_args__ = (
        UniqueConstraint("owner_id", "slug", name="uq_products_owner_slug"),
        UniqueConstraint("owner_id", "sku", name="uq_products_owner_sku"),
        Index("ix_products_name_lower", text("lower(name)")),
        Index("ix_products_owner_created", "owner_id", "created_at"),
        Index("ix_products_status_currency", "status", "currency"),
        Index("ix_products_publish_status", "published_at", "status"),
        CheckConstraint(
            "price >= 0 AND compare_at_price >= 0 AND cost >= 0",
            name="ck_product_prices_nonneg",
        ),
        CheckConstraint(
            "stock_on_hand >= 0 AND stock_reserved >= 0 AND safety_stock >= 0",
            name="ck_product_stock_nonneg",
        ),
        CheckConstraint(
            "length(trim(currency)) = 3 AND currency = upper(currency)",
            name="ck_product_currency_iso3",
        ),
    )


# ───────────── Auto-fill events ─────────────
@listens_for(Product, "before_insert")
def _product_before_insert(_m, _c, target: Product) -> None:  # pragma: no cover
    if not target.slug and target.name:
        target.slug = _slugify(target.name)
    if target.status == ProductStatus.active and not target.published_at:
        target.published_at = _utcnow()
    if target.sku:
        target.sku = target.sku.strip().upper()
    if target.currency:
        target.currency = target.currency.strip().upper()

@listens_for(Product, "before_update")
def _product_before_update(_m, _c, target: Product) -> None:  # pragma: no cover
    if target.slug is None and target.name:
        target.slug = _slugify(target.name)
    if target.status == ProductStatus.active and not target.published_at:
        target.published_at = _utcnow()
    if target.sku:
        target.sku = target.sku.strip().upper()
    if target.currency:
        target.currency = target.currency.strip().upper()

