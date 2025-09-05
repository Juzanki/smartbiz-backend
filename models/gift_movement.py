# backend/models/gift_movement.py
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
    UniqueConstraint,
    text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for
from sqlalchemy.ext.mutable import MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    from .live_stream import LiveStream
    from .gift import Gift


class MovementStatus(str, enum.Enum):
    pending   = "pending"
    succeeded = "succeeded"
    refunded  = "refunded"
    failed    = "failed"
    reversed  = "reversed"   # chargeback/force reversal


class MovementSource(str, enum.Enum):
    inapp_wallet = "inapp_wallet"  # coins/wallet ndani ya app
    external     = "external"      # mpesa/stripe/paypal n.k.
    promo        = "promo"         # bonasi/kuponi
    other        = "other"


class GiftMovement(Base):
    """
    Kumbukumbu ya tukio la zawadi (gift) kwenye live stream.

    • Inaunganisha stream, mtumaji (sender), mwenye stream (host), na aina ya gift
    • Dedupe kupitia idempotency_key
    • Money columns (coins) ni DECIMAL portable (NUMERIC(18,2) kwenye PG)
    """
    __tablename__ = "gift_movements"
    __mapper_args__ = {"eager_defaults": True}

    # ---------------- Keys ----------------
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=False, index=True, doc="Target livestream."
    )
    sender_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True, doc="Gifter (nullable for guests)."
    )
    host_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True, doc="Receiving host."
    )
    gift_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("gifts.id", ondelete="SET NULL"),
        nullable=True, index=True, doc="Concrete gift record."
    )

    # ---------------- Business fields ----------------
    # Kiasi cha “coins” kwa kila kipande cha gift (money-safe)
    unit_coins: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    # Idadi ya vipande vya gift vilivyotumwa kwenye tukio hili
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    # Back-compat: amount ni “coins total” ya zamani—tunaibakiza kwa urahisi wa migration/analytics.
    # Itawekwa auto kwa (unit_coins * quantity) wakati wa insert/update kama huta-set mwenyewe.
    amount: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    currency: Mapped[Optional[str]] = mapped_column(String(8))  # ISO (SBZ/TZS/USD…), hiari kama ni “coins”
    gift_code: Mapped[Optional[str]] = mapped_column(String(50))  # sku/kodi ya gift

    source: Mapped[MovementSource] = mapped_column(
        SQLEnum(MovementSource, name="gift_movement_source", native_enum=False, validate_strings=True),
        default=MovementSource.inapp_wallet,
        nullable=False,
        index=True,
    )
    status: Mapped[MovementStatus] = mapped_column(
        SQLEnum(MovementStatus, name="gift_movement_status", native_enum=False, validate_strings=True),
        default=MovementStatus.succeeded,
        nullable=False,
        index=True,
    )

    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))
    refundable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))

    # Refund audit
    refunded_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    refund_reason: Mapped[Optional[str]] = mapped_column(String(200))

    # Correlation / dedupe
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)

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

    # ---------------- Relationships ----------------
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream",
        primaryjoin="foreign(GiftMovement.stream_id) == LiveStream.id",
        foreign_keys=[stream_id],
        back_populates="gift_movements",
        passive_deletes=True,
        lazy="selectin",
    )
    sender: Mapped[Optional["User"]] = relationship(
        "User",
        primaryjoin="foreign(GiftMovement.sender_id) == User.id",
        foreign_keys=[sender_id],
        back_populates="gift_movements_sent",
        passive_deletes=True,
        lazy="selectin",
    )
    host: Mapped[Optional["User"]] = relationship(
        "User",
        primaryjoin="foreign(GiftMovement.host_id) == User.id",
        foreign_keys=[host_id],
        back_populates="gift_movements_received",
        passive_deletes=True,
        lazy="selectin",
    )
    gift: Mapped[Optional["Gift"]] = relationship(
        "Gift",
        primaryjoin="foreign(GiftMovement.gift_id) == Gift.id",
        foreign_keys=[gift_id],
        back_populates="movements",
        passive_deletes=True,
        lazy="selectin",
    )

    # ---------------- Hybrids ----------------
    @hybrid_property
    def is_anonymous(self) -> bool:
        return self.sender_id is None

    @hybrid_property
    def total_coins(self) -> Decimal:
        """unit_coins * quantity (money-safe)."""
        q = Decimal(self.quantity or 0)
        u = self.unit_coins or Decimal("0")
        return (u * q).quantize(Decimal("1."))  # kwa UI unaweza kuhitaji integer coins

    @hybrid_property
    def can_refund(self) -> bool:
        return bool(self.refundable and self.status == MovementStatus.succeeded)

    # ---------------- Validators / normalizers ----------------
    @validates("currency")
    def _v_currency(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().upper()
        if v and not (2 <= len(v) <= 8):
            raise ValueError("currency must be 2..8 chars")
        return v or None

    @validates("gift_code")
    def _v_gift_code(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return " ".join(v.strip().split()) or None

    @validates("quantity")
    def _v_quantity(self, _k: str, v: int) -> int:
        iv = int(v or 1)
        if iv < 1:
            raise ValueError("quantity must be >= 1")
        return iv

    @validates("unit_coins")
    def _v_unit_coins(self, _k: str, v: Decimal | int | float | str) -> Decimal:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
        if d < 0:
            raise ValueError("unit_coins must be >= 0")
        return d

    @validates("amount")
    def _v_amount(self, _k: str, v: int) -> int:
        iv = int(v or 0)
        if iv < 0:
            raise ValueError("amount must be >= 0")
        return iv

    # ---------------- Helpers ----------------
    def set_amount(self, *, unit: Decimal | int | float | str, quantity: int = 1) -> None:
        """Set unit_coins & quantity; synchronize legacy integer `amount`."""
        self.unit_coins = unit if isinstance(unit, Decimal) else Decimal(str(unit))
        self.quantity = quantity
        # legacy int amount for fast aggregations
        self.amount = int(max(0, (self.unit_coins * Decimal(self.quantity)).to_integral_value(rounding="ROUND_DOWN")))

    def anonymize_sender(self) -> None:
        self.sender_id = None

    def assign_host(self, user_id: Optional[int]) -> None:
        self.host_id = user_id

    def mark_refundable(self, on: bool = True) -> None:
        self.refundable = bool(on)

    def mark_refunded(self, reason: Optional[str] = None) -> None:
        if not self.can_refund:
            raise ValueError("movement not refundable in current state")
        self.status = MovementStatus.refunded
        self.refunded_at = dt.datetime.now(dt.timezone.utc)
        self.refund_reason = (reason or "")[:200] or None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "stream_id": self.stream_id,
            "sender_id": self.sender_id,
            "host_id": self.host_id,
            "gift_id": self.gift_id,
            "unit_coins": str(self.unit_coins),
            "quantity": self.quantity,
            "total_coins": str(self.total_coins),
            "amount": self.amount,
            "currency": self.currency,
            "gift_code": self.gift_code,
            "status": self.status.value,
            "source": self.source.value,
            "idempotency_key": self.idempotency_key,
            "request_id": self.request_id,
            "refundable": bool(self.refundable),
            "refunded_at": self.refunded_at.isoformat() if self.refunded_at else None,
            "refund_reason": self.refund_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<GiftMovement id={self.id} stream={self.stream_id} "
            f"sender={self.sender_id} host={self.host_id} gift={self.gift_id} "
            f"qty={self.quantity} unit={self.unit_coins} total={self.total_coins}>"
        )


# ---------------- Normalizers / denormalized amount ----------------
@listens_for(GiftMovement, "before_insert")
def _gm_before_insert(_m, _c, t: GiftMovement) -> None:  # pragma: no cover
    if t.currency:
        t.currency = t.currency.strip().upper()
    if t.gift_code:
        t.gift_code = " ".join(t.gift_code.strip().split())
    # sync legacy int `amount` kwa usahihi kama haijapangwa
    if not t.amount:
        total = (t.unit_coins or Decimal("0")) * Decimal(t.quantity or 0)
        t.amount = int(max(0, total.to_integral_value(rounding="ROUND_DOWN")))


@listens_for(GiftMovement, "before_update")
def _gm_before_update(_m, _c, t: GiftMovement) -> None:  # pragma: no cover
    if t.currency:
        t.currency = t.currency.strip().upper()
    if t.gift_code:
        t.gift_code = " ".join(t.gift_code.strip().split())
    # endelea kusawazisha `amount`
    total = (t.unit_coins or Decimal("0")) * Decimal(t.quantity or 0)
    t.amount = int(max(0, total.to_integral_value(rounding="ROUND_DOWN")))


# ---------------- Indexes & Constraints ----------------
GiftMovement.__table_args__ = (
    CheckConstraint("quantity >= 1", name="ck_gm_qty_min"),
    CheckConstraint("amount >= 0", name="ck_gm_amount_nonneg"),
    CheckConstraint("unit_coins >= 0", name="ck_gm_unit_nonneg"),
    CheckConstraint("(currency IS NULL) OR (length(currency) BETWEEN 2 AND 8)", name="ck_gm_currency_len"),
    UniqueConstraint("idempotency_key", name="uq_gm_idempotency"),
    Index("ix_gm_stream_time", GiftMovement.stream_id, GiftMovement.created_at),
    Index("ix_gm_sender_time", GiftMovement.sender_id, GiftMovement.created_at),
    Index("ix_gm_host_time", GiftMovement.host_id, GiftMovement.created_at),
    Index("ix_gm_stream_sender", GiftMovement.stream_id, GiftMovement.sender_id),
    Index("ix_gm_stream_host", GiftMovement.stream_id, GiftMovement.host_id),
    Index("ix_gm_reqid", GiftMovement.request_id),
    Index("ix_gm_amount", GiftMovement.amount),
    Index("ix_gm_gift_id_time", GiftMovement.gift_id, GiftMovement.created_at),
    Index("ix_gm_status_source", GiftMovement.status, GiftMovement.source),
    Index("ix_gm_gift_code_lower", func.lower(GiftMovement.gift_code)),
)
