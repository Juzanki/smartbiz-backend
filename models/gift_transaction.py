# backend/models/gift_transaction.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from decimal import Decimal
from typing import Optional, TYPE_CHECKING, Dict, Any

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
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    from .gift import Gift
    from .live_stream import LiveStream
    from .gift_movement import GiftMovement


# ───────────────── Enums ─────────────────
class GiftTxnStatus(str, enum.Enum):
    pending = "pending"
    settled = "settled"
    refunded = "refunded"
    failed = "failed"
    canceled = "canceled"


class GiftTxnSource(str, enum.Enum):
    inapp = "inapp"  # wallet/SmartCoins
    stripe = "stripe"
    mpesa = "mpesa"
    airtel = "airtel"
    paypal = "paypal"
    other = "other"


class GiftCurrency(str, enum.Enum):
    SC = "SC"  # SmartCoins (in-app)
    TZS = "TZS"
    USD = "USD"
    EUR = "EUR"
    OTHER = "OTHER"


# ───────────────── Model ─────────────────
class GiftTransaction(Base):
    """
    Muamala wa 'gift' kati ya mtumaji na mpokeaji, ukiwa na snapshot ya bei/kiasi
    na viungo vya hiari kwa LiveStream/Gift/GiftMovement.
    """
    __tablename__ = "gift_transactions"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        # Uniqueness & lookups
        UniqueConstraint("idempotency_key", name="uq_gift_txn_idem"),
        Index("ix_gift_txn_sender_time", "sender_id", "created_at"),
        Index("ix_gift_txn_recipient_time", "recipient_id", "created_at"),
        Index("ix_gift_txn_status_time", "status", "created_at"),
        Index("ix_gift_txn_stream_time", "stream_id", "created_at"),
        Index("ix_gift_txn_source_currency", "source", "currency"),
        Index("ix_gift_txn_sender_recipient", "sender_id", "recipient_id", "created_at"),
        # Integrity
        CheckConstraint("quantity >= 1", name="ck_gift_txn_qty_min"),
        CheckConstraint("unit_coins >= 0", name="ck_gift_txn_unit_nonneg"),
        CheckConstraint("total_coins >= 0", name="ck_gift_txn_total_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Parties
    sender_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Optional linkages
    stream_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("live_streams.id", ondelete="SET NULL"), index=True
    )
    gift_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("gifts.id", ondelete="SET NULL"), index=True
    )
    movement_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("gift_movements.id", ondelete="SET NULL"), index=True
    )

    # Snapshot ya bidhaa/bei
    gift_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    gift_slug: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    # Fedha (Decimal; portable NUMERIC/DECIMAL kupitia DECIMAL_TYPE)
    unit_coins: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    total_coins: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    currency: Mapped[GiftCurrency] = mapped_column(
        SQLEnum(GiftCurrency, name="gift_txn_currency", native_enum=False, validate_strings=True),
        default=GiftCurrency.SC,
        nullable=False,
        index=True,
    )
    source: Mapped[GiftTxnSource] = mapped_column(
        SQLEnum(GiftTxnSource, name="gift_txn_source", native_enum=False, validate_strings=True),
        default=GiftTxnSource.inapp,
        nullable=False,
        index=True,
    )
    status: Mapped[GiftTxnStatus] = mapped_column(
        SQLEnum(GiftTxnStatus, name="gift_txn_status", native_enum=False, validate_strings=True),
        default=GiftTxnStatus.pending,
        nullable=False,
        index=True,
    )

    # Metadata / audit
    message: Mapped[Optional[str]] = mapped_column(String(240))
    reference: Mapped[Optional[str]] = mapped_column(String(100), index=True)  # ext ref (payment/order id)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    settled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # -------- Relationships (loose; no back_populates to avoid tight coupling) --------
    sender: Mapped["User"] = relationship("User", foreign_keys=[sender_id], passive_deletes=True, lazy="selectin")
    recipient: Mapped["User"] = relationship("User", foreign_keys=[recipient_id], passive_deletes=True, lazy="selectin")
    stream: Mapped[Optional["LiveStream"]] = relationship("LiveStream", foreign_keys=[stream_id], passive_deletes=True, lazy="selectin")
    gift: Mapped[Optional["Gift"]] = relationship("Gift", foreign_keys=[gift_id], lazy="selectin")
    movement: Mapped[Optional["GiftMovement"]] = relationship("GiftMovement", foreign_keys=[movement_id], lazy="selectin")

    # -------- Hybrids --------
    @hybrid_property
    def total_value(self) -> Decimal:
        return self.total_coins or Decimal("0")

    @hybrid_property
    def is_terminal(self) -> bool:
        return self.status in (
            GiftTxnStatus.settled,
            GiftTxnStatus.refunded,
            GiftTxnStatus.failed,
            GiftTxnStatus.canceled,
        )

    # -------- Helpers --------
    def set_amount_and_price(self, amount: int, unit: Decimal | int | float | str) -> None:
        amt = max(1, int(amount))
        unit_dec = unit if isinstance(unit, Decimal) else Decimal(str(unit))
        self.quantity = amt
        self.unit_coins = max(Decimal("0"), unit_dec)
        self.total_coins = self.unit_coins * Decimal(amt)

    def snapshot_from_gift(self, gift: "Gift", *, amount: int = 1) -> None:
        """Chukua jina/slug/bei kutoka Gift (snapshot wakati wa muamala)."""
        self.gift_name = gift.name
        self.gift_slug = getattr(gift, "slug", None)
        self.gift_id = getattr(gift, "id", None)
        self.set_amount_and_price(amount, getattr(gift, "coins", 0))

    def mark_settled(self) -> None:
        self.status = GiftTxnStatus.settled
        self.settled_at = dt.datetime.now(dt.timezone.utc)

    def mark_refunded(self) -> None:
        self.status = GiftTxnStatus.refunded
        self.refunded_at = dt.datetime.now(dt.timezone.utc)

    def mark_failed(self) -> None:
        self.status = GiftTxnStatus.failed
        self.failed_at = dt.datetime.now(dt.timezone.utc)

    def cancel(self) -> None:
        self.status = GiftTxnStatus.canceled
        self.canceled_at = dt.datetime.now(dt.timezone.utc)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<GiftTransaction id={self.id} sender={self.sender_id} recipient={self.recipient_id} "
            f"gift={self.gift_name!r} x{self.quantity} total={self.total_coins} status={self.status}>"
        )

