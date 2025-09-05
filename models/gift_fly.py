# backend/models/gift_fly.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING, Any

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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableDict

from sqlalchemy.event import listens_for

from backend.db import Base
# Portable types from your shared module (PG→JSONB/NUMERIC; others→JSON/Numeric)
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE

if TYPE_CHECKING:
    from .user import User
    from .live_stream import LiveStream  # __tablename__="live_streams"


# -------- Enums --------
class GiftSource(str, enum.Enum):
    inapp  = "inapp"      # app wallet / coins
    stripe = "stripe"
    mpesa  = "mpesa"
    airtel = "airtel"
    tigo   = "tigo"
    paypal = "paypal"
    other  = "other"


class Currency(str, enum.Enum):
    TZS = "TZS"
    USD = "USD"
    EUR = "EUR"
    OTHER = "OTHER"


# -------- Model --------
class GiftFly(Base):
    """
    'Gift fly' event during a live stream.
    Tracks who gifted, what, how much, and any animation/UX metadata.

    Design goals:
    - Cross-DB safe money columns via DECIMAL_TYPE
    - Mutable JSON for `animation`/`meta` (in-place updates are tracked)
    - Idempotency key to dedupe double posts from gateways/webhooks
    - Helpful hybrids & helpers for common app logic
    """
    __tablename__ = "gift_fly_events"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Where & by whom
    stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # Gift identity
    gift_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    gift_code: Mapped[Optional[str]] = mapped_column(String(64), index=True)  # e.g. "ROSE"
    message:   Mapped[Optional[str]] = mapped_column(String(240))
    image_url: Mapped[Optional[str]] = mapped_column(String(512))             # optional art/thumbnail

    # Amount & currency
    unit_value: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    quantity:   Mapped[int]     = mapped_column(Integer, nullable=False, server_default=text("1"))
    currency:   Mapped[Currency] = mapped_column(
        SQLEnum(Currency, name="gift_currency", native_enum=False, validate_strings=True),
        default=Currency.TZS,
        nullable=False,
        index=True,
    )
    source:     Mapped[GiftSource] = mapped_column(
        SQLEnum(GiftSource, name="gift_source", native_enum=False, validate_strings=True),
        default=GiftSource.inapp,
        nullable=False,
        index=True,
    )

    # Animation & misc (Mutable so in-place changes are tracked by SQLAlchemy)
    animation: Mapped[Optional[dict[str, Any]]] = mapped_column(
        MutableDict.as_mutable(JSON_VARIANT)
    )  # {"effect":"fly-neon","duration_ms":1800,...}

    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(
        MutableDict.as_mutable(JSON_VARIANT)
    )  # {"order_id":"...", "wallet_tx":"...", "locale":"..."} etc.

    # Idempotency / dedupe (per event)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(100), unique=True, index=True)

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

    # Relationships
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream",
        back_populates="gift_fly_events",
        foreign_keys=[stream_id],
        passive_deletes=True,
        lazy="selectin",
    )
    user: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="gift_fly_events",
        foreign_keys=[user_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # -------- Hybrids / helpers --------
    @hybrid_property
    def total_value(self) -> Decimal:
        """Compute total = unit_value * quantity, rounded to 2dp for display."""
        unit = self.unit_value or Decimal("0")
        qty = Decimal(self.quantity or 0)
        return (unit * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @total_value.expression
    def total_value(cls):
        # SQL side (no rounding—leave to DB engine / reporting)
        return cls.unit_value * cls.quantity

    @hybrid_property
    def is_anonymous(self) -> bool:
        return self.user_id is None

    # ------- Validators -------
    @validates("quantity")
    def _validate_qty(self, _key, value: int) -> int:
        iv = int(value or 1)
        if iv < 1:
            raise ValueError("quantity must be >= 1")
        if iv > 1_000_000:
            raise ValueError("quantity looks unrealistic")
        return iv

    @validates("unit_value")
    def _validate_unit(self, _key, value: Decimal | int | float | str) -> Decimal:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
        if d < 0:
            raise ValueError("unit_value must be >= 0")
        # Normalize to 2dp for money display/consistency
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @validates("gift_name", "gift_code", "message", "image_url")
    def _trim_strs(self, key: str, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        v = value.strip()
        maxlen = {"gift_name": 120, "gift_code": 64, "message": 240, "image_url": 512}[key]
        return v[:maxlen] or None

    # ------- Domain helpers -------
    def set_amount(self, unit: Decimal | int | float | str, qty: int = 1) -> None:
        self.unit_value = unit
        self.quantity = qty

    def set_idempotency(self, key: Optional[str]) -> None:
        self.idempotency_key = (key or "").strip()[:100] or None

    def anonymize(self) -> None:
        self.user_id = None

    def merge_animation(self, **kwargs: Any) -> None:
        """Shorthand to update animation JSON safely."""
        self.animation = {**(self.animation or {}), **kwargs}

    def merge_meta(self, **kwargs: Any) -> None:
        """Shorthand to update meta JSON safely."""
        self.meta = {**(self.meta or {}), **kwargs}

    def as_dict(self) -> dict[str, Any]:
        """Lightweight serializer for real-time events/sockets."""
        return {
            "id": self.id,
            "stream_id": self.stream_id,
            "user_id": self.user_id,
            "gift_name": self.gift_name,
            "gift_code": self.gift_code,
            "message": self.message,
            "image_url": self.image_url,
            "unit_value": str(self.unit_value or 0),
            "quantity": self.quantity,
            "total_value": str(self.total_value),
            "currency": self.currency.value,
            "source": self.source.value,
            "animation": self.animation or {},
            "meta": self.meta or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<GiftFly id={self.id} stream={self.stream_id} user={self.user_id} "
            f"gift={self.gift_name!r} x{self.quantity} {self.currency} unit={self.unit_value}>"
        )

    # ------- Constraints & Indexes -------
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_gfe_idem_key"),
        Index("ix_gfe_stream_time", "stream_id", "created_at"),
        Index("ix_gfe_user_time", "user_id", "created_at"),
        Index("ix_gfe_source_currency", "source", "currency"),
        Index("ix_gfe_gift_lower", func.lower(gift_name)),  # case-insensitive search
        CheckConstraint("quantity >= 1", name="ck_gfe_qty_min"),
        CheckConstraint("unit_value >= 0", name="ck_gfe_value_nonneg"),
        CheckConstraint("length(trim(gift_name)) >= 2", name="ck_gfe_name_len"),
    )


# -------- Normalizers (events) --------
@listens_for(GiftFly, "before_insert")
def _giftfly_before_insert(_mapper, _conn, target: GiftFly) -> None:  # pragma: no cover
    # clamp strings (already handled in validators, but safe if direct assignment bypassed)
    if target.gift_name:
        target.gift_name = target.gift_name.strip()[:120]
    if target.gift_code:
        target.gift_code = target.gift_code.strip()[:64]
    if target.message:
        target.message = target.message.strip()[:240]
    if target.image_url:
        target.image_url = target.image_url.strip()[:512]
    # normalize numbers
    target.quantity = int(target.quantity or 1)
    if target.unit_value is not None and not isinstance(target.unit_value, Decimal):
        target.unit_value = Decimal(str(target.unit_value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@listens_for(GiftFly, "before_update")
def _giftfly_before_update(_mapper, _conn, target: GiftFly) -> None:  # pragma: no cover
    # keep normalization consistent on updates
    if target.unit_value is not None and not isinstance(target.unit_value, Decimal):
        target.unit_value = Decimal(str(target.unit_value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
