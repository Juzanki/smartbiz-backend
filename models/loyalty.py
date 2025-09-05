# backend/models/loyalty.py
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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json  # portable JSON/JSONB

# --------- Cross-DB NUMERIC for points (18,4) ---------
try:
    from sqlalchemy.dialects.postgresql import NUMERIC as PG_NUMERIC  # type: ignore
    POINTS_TYPE = PG_NUMERIC(18, 4)
except Exception:  # pragma: no cover
    from sqlalchemy import Numeric as SA_NUMERIC
    POINTS_TYPE = SA_NUMERIC(18, 4)

if TYPE_CHECKING:
    from .user import User


# --------------------- Enums ---------------------
class LoyaltyReason(str, enum.Enum):
    purchase   = "purchase"
    referral   = "referral"
    promo      = "promo"
    bonus      = "bonus"
    adjustment = "adjustment"
    redeem     = "redeem"
    reversal   = "reversal"
    expiration = "expiration"
    other      = "other"


class LoyaltyStatus(str, enum.Enum):
    pending  = "pending"
    posted   = "posted"
    reversed = "reversed"
    expired  = "expired"
    canceled = "canceled"


class LoyaltyChannel(str, enum.Enum):
    web    = "web"
    mobile = "mobile"
    api    = "api"
    admin  = "admin"
    other  = "other"


# --------------------- Model ---------------------
class LoyaltyPoint(Base):
    """
    Ledger ya pointi za uaminifu (credit/debit), audit-grade.
    - `points_delta` inaweza kuwa +ve (credit) au -ve (debit).
    - `balance_after` ni snapshot ya baada ya tukio hili (hiari).
    - `idempotency_key` kuzuia uandishi marudio.
    """
    __tablename__ = "loyalty_points"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Beneficiary
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user: Mapped["User"] = relationship(
        "User",
        back_populates="loyalty_points",
        foreign_keys=[user_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # Uainishaji
    reason: Mapped[LoyaltyReason] = mapped_column(
        SQLEnum(LoyaltyReason, name="loyalty_reason"),
        default=LoyaltyReason.other,
        nullable=False,
        index=True,
    )
    status: Mapped[LoyaltyStatus] = mapped_column(
        SQLEnum(LoyaltyStatus, name="loyalty_status"),
        default=LoyaltyStatus.posted,
        nullable=False,
        index=True,
    )
    channel: Mapped[LoyaltyChannel] = mapped_column(
        SQLEnum(LoyaltyChannel, name="loyalty_channel"),
        default=LoyaltyChannel.web,
        nullable=False,
        index=True,
    )

    # Kiasi & snapshot (18,4)
    points_delta:   Mapped[Decimal] = mapped_column(POINTS_TYPE, nullable=False)
    balance_after:  Mapped[Optional[Decimal]] = mapped_column(POINTS_TYPE)

    # Uhusiano/vidokezo vya nje
    reference:         Mapped[Optional[str]] = mapped_column(String(100), index=True)  # order/payment/referral code
    idempotency_key:   Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)

    awarded_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        default=None,
    )
    awarded_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[awarded_by_user_id],
        lazy="selectin",
    )

    # Maelezo & meta
    note: Mapped[Optional[str]] = mapped_column(String(240))
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))  # {"order_id":"...","campaign":"..."}

    # Muda wa uhalali/lifecycle
    expires_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    created_at:  Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at:  Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    posted_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    reversed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    expired_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # --------- Hybrids ---------
    @hybrid_property
    def is_credit(self) -> bool:
        return (self.points_delta or Decimal("0")) > 0

    @hybrid_property
    def is_debit(self) -> bool:
        return (self.points_delta or Decimal("0")) < 0

    # --------- Helpers (domain) ---------
    def credit(self, amount: Decimal | int | float | str) -> None:
        amt = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        self.points_delta = max(Decimal("0.0000"), amt)
        self.status = LoyaltyStatus.posted
        self.posted_at = dt.datetime.now(dt.timezone.utc)

    def debit(self, amount: Decimal | int | float | str) -> None:
        amt = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        self.points_delta = -max(Decimal("0.0000"), amt)
        self.status = LoyaltyStatus.posted
        self.posted_at = dt.datetime.now(dt.timezone.utc)

    def reverse(self, *, note: str | None = None) -> None:
        self.status = LoyaltyStatus.reversed
        self.reversed_at = dt.datetime.now(dt.timezone.utc)
        if note:
            self.note = ((self.note + " | ") if self.note else "") + note.strip()

    def expire(self) -> None:
        self.status = LoyaltyStatus.expired
        self.expired_at = dt.datetime.now(dt.timezone.utc)

    def cancel(self, *, note: str | None = None) -> None:
        self.status = LoyaltyStatus.canceled
        self.canceled_at = dt.datetime.now(dt.timezone.utc)
        if note:
            self.note = ((self.note + " | ") if self.note else "") + note.strip()

    def apply_snapshot(self, current_balance: Decimal | int | float | str) -> None:
        bal = current_balance if isinstance(current_balance, Decimal) else Decimal(str(current_balance))
        self.balance_after = bal

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<LoyaltyPoint id={self.id} user={self.user_id} "
            f"delta={self.points_delta} status={self.status} reason={self.reason}>"
        )

    # --------- Indices & Constraints ---------
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_loy_idem_key"),
        Index("ix_loy_user_created", "user_id", "created_at"),
        Index("ix_loy_status_time", "status", "created_at"),
        Index("ix_loy_reason", "reason"),
        Index("ix_loy_expiry", "expires_at"),
        Index("ix_loy_reference", "reference"),
        # Zuia 0 na weka kikomo salama (−1e9..+1e9) ili kuzuia makosa ya kipimo
        CheckConstraint("points_delta <> 0", name="ck_loy_delta_nonzero"),
        CheckConstraint("points_delta >= -1000000000 AND points_delta <= 1000000000", name="ck_loy_delta_bounds"),
        # Tarehe za tukio lazima zilingane na status (guards nyepesi, cross-DB)
        CheckConstraint(
            "(status <> 'posted') OR (posted_at IS NOT NULL)",
            name="ck_loy_posted_has_ts",
        ),
        CheckConstraint(
            "(status <> 'reversed') OR (reversed_at IS NOT NULL)",
            name="ck_loy_reversed_has_ts",
        ),
        CheckConstraint(
            "(status <> 'expired') OR (expired_at IS NOT NULL)",
            name="ck_loy_expired_has_ts",
        ),
        {"extend_existing": True},
    )


# ---------------- Validators / Normalizers ----------------
@validates("reference", "note", "idempotency_key")
def _trim_texts(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    return v or None


# ---------------- Event hooks ----------------
from sqlalchemy.event import listens_for  # keep local to avoid circulars

@listens_for(LoyaltyPoint, "before_insert")
def _lp_before_insert(_m, _c, t: LoyaltyPoint) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    # Set timestamps kulingana na status kama hazijawekwa
    if t.status == LoyaltyStatus.posted and not t.posted_at:
        t.posted_at = now
    if t.status == LoyaltyStatus.reversed and not t.reversed_at:
        t.reversed_at = now
    if t.status == LoyaltyStatus.expired and not t.expired_at:
        t.expired_at = now
    if t.status == LoyaltyStatus.canceled and not t.canceled_at:
        t.canceled_at = now
    # Normalize short texts
    if t.reference:
        t.reference = t.reference.strip() or None
    if t.note:
        t.note = t.note.strip() or None
    if t.idempotency_key:
        t.idempotency_key = t.idempotency_key.strip() or None


@listens_for(LoyaltyPoint, "before_update")
def _lp_before_update(_m, _c, t: LoyaltyPoint) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    if t.status == LoyaltyStatus.posted and not t.posted_at:
        t.posted_at = now
    if t.status == LoyaltyStatus.reversed and not t.reversed_at:
        t.reversed_at = now
    if t.status == LoyaltyStatus.expired and not t.expired_at:
        t.expired_at = now
    if t.status == LoyaltyStatus.canceled and not t.canceled_at:
        t.canceled_at = now
