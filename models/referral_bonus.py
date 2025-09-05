# backend/models/referral_bonus.py
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
    Text,
    UniqueConstraint,
    func,
    text,
    JSON as SA_JSON,
    Numeric as SA_NUMERIC,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# ---- Portable types ----
try:
    # NUMERIC(18,2) kwenye PG; huanguka kwenye generic Numeric vinginevyo
    from sqlalchemy.dialects.postgresql import NUMERIC as PG_NUMERIC  # type: ignore
    DECIMAL_TYPE = PG_NUMERIC(18, 2)
except Exception:  # pragma: no cover
    DECIMAL_TYPE = SA_NUMERIC(18, 2)

if TYPE_CHECKING:
    from .user import User
    from .referral_log import ReferralLog
    from .campaign import Campaign
    # from .smart_coin_transaction import SmartCoinTransaction

# --------- Enums ---------
class BonusKind(str, enum.Enum):
    signup     = "signup"
    purchase   = "purchase"
    milestone  = "milestone"
    campaign   = "campaign"
    manual     = "manual"
    other      = "other"

class BonusUnit(str, enum.Enum):
    coins    = "coins"
    currency = "currency"
    points   = "points"

class BonusStatus(str, enum.Enum):
    pending   = "pending"
    granted   = "granted"
    redeemed  = "redeemed"
    canceled  = "canceled"
    expired   = "expired"
    reversed  = "reversed"

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class ReferralBonus(Base):
    """
    Bonus ya marejeleo (referral) kwa aliye-refer.

    Sifa kuu:
    - unit: coins | currency | points
    - amount: Decimal(18,2) (kwa points pia, kiasi kinaweza kuwa “isiyo ya fedha” lakini hutunzwa sawa)
    - currency (hiari) hutumika tu ukiwa kwenye unit=currency
    - points_scale (hiari) hutumika kubadili points→coins (mf. 100 points = 1 coin)
    - lifecycle helpers: grant/redeem/cancel/expire/reverse
    - conversion helpers: as_coins(), as_currency(), as_points()
    """
    __tablename__ = "referral_bonuses"
    __mapper_args__ = {"eager_defaults": True}

    # ----- Uniques & index -----
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_refbonus_idem"),
        UniqueConstraint("referral_log_id", name="uq_refbonus_referral_log"),
        Index("ix_refbonus_referrer_created", "referrer_id", "created_at"),
        Index("ix_refbonus_status_time", "status", "created_at"),
        Index("ix_refbonus_kind_unit", "kind", "unit"),
        Index("ix_refbonus_referred", "referred_user_id"),
        CheckConstraint("amount >= 0", name="ck_refbonus_amount_nonneg"),
        CheckConstraint("currency IS NULL OR length(currency) = 3", name="ck_refbonus_currency_iso3"),
        CheckConstraint("points_scale IS NULL OR points_scale >= 1", name="ck_refbonus_points_scale_min1"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Beneficiary (referrer)
    referrer_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    referrer: Mapped["User"] = relationship(
        "User",
        foreign_keys=[referrer_id],
        back_populates="referral_bonuses_made",
        passive_deletes=True,
        lazy="selectin",
    )

    # (Optional) Referred user
    referred_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    referred_user: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[referred_user_id],
        back_populates="referral_bonuses_received",
        passive_deletes=True,
        lazy="selectin",
    )

    # (Optional) Source log/campaign
    referral_log_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("referral_logs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    referral_log: Mapped[Optional["ReferralLog"]] = relationship(
        "ReferralLog", lazy="selectin", passive_deletes=True
    )

    campaign_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True, nullable=True
    )
    campaign: Mapped[Optional["Campaign"]] = relationship(
        "Campaign", lazy="selectin", passive_deletes=True
    )

    # Classification
    kind: Mapped[BonusKind] = mapped_column(
        SQLEnum(BonusKind, name="ref_bonus_kind", native_enum=False, validate_strings=True),
        default=BonusKind.signup,
        nullable=False,
        index=True,
    )
    unit: Mapped[BonusUnit] = mapped_column(
        SQLEnum(BonusUnit, name="ref_bonus_unit", native_enum=False, validate_strings=True),
        default=BonusUnit.coins,
        nullable=False,
        index=True,
    )
    status: Mapped[BonusStatus] = mapped_column(
        SQLEnum(BonusStatus, name="ref_bonus_status", native_enum=False, validate_strings=True),
        default=BonusStatus.pending,
        nullable=False,
        index=True,
    )

    # Details
    bonus_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    # Amount
    amount: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    currency: Mapped[Optional[str]] = mapped_column(String(3), index=True)  # used if unit == currency
    points_scale: Mapped[Optional[int]] = mapped_column(Integer)  # e.g., 100 => 100 points = 1 coin

    # Link to SmartCoinTransaction (optional)
    smart_coin_tx_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("smart_coin_transactions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # smart_coin_tx: Mapped[Optional["SmartCoinTransaction"]] = relationship("SmartCoinTransaction", lazy="selectin")

    # Dedupe / refs
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    external_ref:    Mapped[Optional[str]] = mapped_column(String(160), index=True)

    # Meta
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    granted_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    redeemed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    expired_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    reversed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # ---------- Hybrids ----------
    @hybrid_property
    def is_active(self) -> bool:
        return self.status in {BonusStatus.pending, BonusStatus.granted}

    @hybrid_property
    def is_terminal(self) -> bool:
        return self.status in {
            BonusStatus.redeemed, BonusStatus.canceled, BonusStatus.expired, BonusStatus.reversed
        }

    # ---------- Validators ----------
    @validates("currency")
    def _v_currency(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v2 = v.strip().upper()
        return v2[:3] or None

    @validates("bonus_name", "description", "idempotency_key", "external_ref")
    def _v_trim(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    # ---------- Conversion helpers ----------
    def as_coins(self, *, fx_rate_currency_to_coin: Optional[Decimal] = None) -> Decimal:
        """
        Rudisha thamani katika COINS:
        - unit=coins  -> amount
        - unit=currency -> amount * fx_rate_currency_to_coin (ikipewa), vinginevyo 0
        - unit=points  -> amount / max(points_scale,1)
        """
        amt = self.amount or Decimal("0")
        if self.unit == BonusUnit.coins:
            return amt
        if self.unit == BonusUnit.currency:
            if fx_rate_currency_to_coin is None or fx_rate_currency_to_coin <= 0:
                return Decimal("0")
            return (amt * fx_rate_currency_to_coin).quantize(Decimal("0.01"))
        # points
        scale = Decimal(str(self.points_scale or 1))
        return (amt / (scale if scale > 0 else Decimal("1"))).quantize(Decimal("0.01"))

    def as_currency(self, *, fx_rate_coin_to_currency: Optional[Decimal] = None) -> Decimal:
        """
        Rudisha thamani katika CURRENCY (sarafu ya `currency`):
        - unit=currency -> amount
        - unit=coins -> amount * fx_rate_coin_to_currency
        - unit=points -> (points/scale) * fx_rate_coin_to_currency
        """
        amt = self.amount or Decimal("0")
        if self.unit == BonusUnit.currency:
            return amt
        if fx_rate_coin_to_currency is None or fx_rate_coin_to_currency <= 0:
            return Decimal("0")
        if self.unit == BonusUnit.coins:
            return (amt * fx_rate_coin_to_currency).quantize(Decimal("0.01"))
        # points
        scale = Decimal(str(self.points_scale or 1))
        coins = amt / (scale if scale > 0 else Decimal("1"))
        return (coins * fx_rate_coin_to_currency).quantize(Decimal("0.01"))

    def as_points(self) -> Decimal:
        """
        Rudisha points:
        - unit=points -> amount
        - unit=coins  -> amount * points_scale
        - unit=currency -> 0 (isipokuwa ukitumia sera ya kubadilisha fedha→points kwenye application layer)
        """
        amt = self.amount or Decimal("0")
        if self.unit == BonusUnit.points:
            return amt
        scale = Decimal(str(self.points_scale or 1))
        if scale <= 0:
            scale = Decimal("1")
        if self.unit == BonusUnit.coins:
            return (amt * scale).quantize(Decimal("0.01"))
        return Decimal("0")

    # ---------- Lifecycle helpers ----------
    def grant(self) -> None:
        self.status = BonusStatus.granted
        self.granted_at = _utcnow()

    def redeem(self) -> None:
        self.status = BonusStatus.redeemed
        self.redeemed_at = _utcnow()

    def cancel(self) -> None:
        self.status = BonusStatus.canceled
        self.canceled_at = _utcnow()

    def expire(self) -> None:
        self.status = BonusStatus.expired
        self.expired_at = _utcnow()

    def reverse(self) -> None:
        """Rejesha/ondoa bonus (chargeback/abuse)."""
        self.status = BonusStatus.reversed
        self.reversed_at = _utcnow()

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ReferralBonus id={self.id} referrer={self.referrer_id} "
            f"referred={self.referred_user_id} kind={self.kind} unit={self.unit} "
            f"amount={self.amount} status={self.status}>"
        )


# ---------- Normalization hooks ----------
@listens_for(ReferralBonus, "before_insert")
def _rb_before_insert(_m, _c, t: ReferralBonus) -> None:  # pragma: no cover
    if t.currency:
        t.currency = (t.currency or "").strip().upper()[:3] or None
    if t.bonus_name:
        t.bonus_name = t.bonus_name.strip()
    # points_scale yasipowekwa kwa unit=points → weka 1
    if t.unit == BonusUnit.points and not t.points_scale:
        t.points_scale = 1

@listens_for(ReferralBonus, "before_update")
def _rb_before_update(_m, _c, t: ReferralBonus) -> None:  # pragma: no cover
    if t.currency:
        t.currency = (t.currency or "").strip().upper()[:3] or None
    if t.bonus_name:
        t.bonus_name = t.bonus_name.strip()
    if t.unit == BonusUnit.points and not t.points_scale:
        t.points_scale = 1
