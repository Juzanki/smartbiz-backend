# backend/models/fan.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Fan model: follower graph between users.

- user_id      -> the follower (the "fan")
- host_user_id -> the creator/host being followed

Inatumika kwa:
- Leaderboards (total_contribution, contribution_count, streaks)
- Perks/tiers (bronze..diamond)
- Moderation (blocked/muted)
"""

import enum
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method
from sqlalchemy.ext.mutable import MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE  # portable (PG: NUMERIC, others: Numeric)

if TYPE_CHECKING:
    from .user import User


# ---------- Enums ----------
class FanStatus(str, enum.Enum):
    active  = "active"
    blocked = "blocked"
    muted   = "muted"


class FanTier(str, enum.Enum):
    bronze   = "bronze"
    silver   = "silver"
    gold     = "gold"
    platinum = "platinum"
    diamond  = "diamond"


# ---------- Helpers ----------
def _money(x: Decimal | int | float | str) -> Decimal:
    """Normalize → Decimal(2dp), salama kwa fedha."""
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------- Model ----------
class Fan(Base):
    """
    Follower edge kati ya mtumiaji (fan) na host (creator).
    Hutoa takwimu za leaderboards, perks na moderation.
    """
    __tablename__ = "fans"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Who follows whom (2x FK -> users.id)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    host_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Money-safe aggregates
    total_contribution: Mapped[Decimal] = mapped_column(
        DECIMAL_TYPE, nullable=False, server_default=text("0")
    )
    contribution_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Streaks
    streak_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_streak_day: Mapped[Optional[dt.date]] = mapped_column(Date)

    # Classification
    status: Mapped[FanStatus] = mapped_column(
        SQLEnum(FanStatus, name="fan_status", native_enum=False, validate_strings=True),
        default=FanStatus.active,
        nullable=False,
        index=True,
    )
    tier: Mapped[FanTier] = mapped_column(
        SQLEnum(FanTier, name="fan_tier", native_enum=False, validate_strings=True),
        default=FanTier.bronze,
        nullable=False,
        index=True,
    )

    # Badges/perks + misc
    last_badge_awarded_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    rank_cache: Mapped[Optional[int]] = mapped_column(Integer)
    meta: Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))

    # Timestamps
    first_contributed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    last_contributed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    # Relationships (two FKs to User) — majina yafanane na upande wa User
    fan: Mapped["User"] = relationship(
        "User",
        foreign_keys=[user_id],
        back_populates="following_hosts",
        passive_deletes=True,
        lazy="selectin",
    )
    host: Mapped["User"] = relationship(
        "User",
        foreign_keys=[host_user_id],
        back_populates="host_followers",
        passive_deletes=True,
        lazy="selectin",
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def is_active_follower(self) -> bool:
        return self.status == FanStatus.active

    @hybrid_property
    def avg_contribution(self) -> Decimal:
        """Wastani kwa kila tukio (0 kama hakuna)."""
        if not self.contribution_count:
            return Decimal("0.00")
        return _money((self.total_contribution or Decimal("0")) / Decimal(self.contribution_count))

    @hybrid_method
    def is_pair(self, u: int, h: int) -> bool:
        return self.user_id == u and self.host_user_id == h

    # ---------- Business helpers ----------
    def add_contribution(self, amount: Decimal | int | float | str) -> None:
        """
        Ongeza mchango; husasisha counters, timestamps na streak.
        Tumia ndani ya transaction pamoja na kuandika BillingLog/Order ukihitaji.
        """
        amt = _money(amount)
        if amt <= 0:
            raise ValueError("amount must be positive")

        now = _utcnow()
        self.total_contribution = _money((self.total_contribution or Decimal("0")) + amt)
        self.contribution_count = (self.contribution_count or 0) + 1

        if not self.first_contributed_at:
            self.first_contributed_at = now
        self.last_contributed_at = now
        self._tick_streak(now.date())

    def _tick_streak(self, today: dt.date) -> None:
        """Maintain a simple daily contribution streak counter."""
        last = self.last_streak_day
        if last is None:
            self.streak_days = 1
        elif today == last:
            # same day, usiongeze streak
            pass
        elif (today - last).days == 1:
            self.streak_days = (self.streak_days or 0) + 1
        else:
            self.streak_days = 1
        self.last_streak_day = today

    def reset_streak(self) -> None:
        self.streak_days = 0
        self.last_streak_day = None

    def promote_tier(self, new_tier: FanTier) -> None:
        self.tier = new_tier
        self.last_badge_awarded_at = _utcnow()

    # Moderation
    def block(self) -> None: self.status = FanStatus.blocked
    def mute(self) -> None:  self.status = FanStatus.muted
    def activate(self) -> None: self.status = FanStatus.active

    # Meta utilities
    def meta_set(self, **items: object) -> None:
        self.meta = {**(self.meta or {}), **items}

    # ---------- Validations ----------
    @validates("total_contribution", "contribution_count", "streak_days")
    def _nonneg(self, _k: str, v):
        # Guards za haraka upande wa ORM
        if v is None:
            return None
        if isinstance(v, (int,)):
            if v < 0:
                raise ValueError(f"{_k} must be non-negative")
            return v
        # money field
        d = _money(v)
        if d < 0:
            raise ValueError(f"{_k} must be non-negative")
        return d

    @validates("user_id", "host_user_id")
    def _no_self_follow(self, _k: str, v: int) -> int:
        # Constraint ipo pia upande wa DB; hii ni ya UX nzuri mapema
        if _k == "host_user_id" and hasattr(self, "user_id") and self.user_id == v:
            raise ValueError("user cannot follow themselves")
        if _k == "user_id" and hasattr(self, "host_user_id") and self.host_user_id == v:
            raise ValueError("user cannot follow themselves")
        return v

    # ---------- Repr ----------
    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Fan id={self.id} user={self.user_id} host={self.host_user_id} "
            f"tier={self.tier} total={self.total_contribution} count={self.contribution_count}>"
        )

    # ---------- Constraints & Indexes ----------
    __table_args__ = (
        UniqueConstraint("user_id", "host_user_id", name="uq_fan_user_host"),
        CheckConstraint("user_id <> host_user_id", name="ck_fan_not_self"),
        CheckConstraint("total_contribution >= 0", name="ck_fan_total_nonneg"),
        CheckConstraint("contribution_count >= 0", name="ck_fan_count_nonneg"),
        CheckConstraint("streak_days >= 0", name="ck_fan_streak_nonneg"),
        Index("ix_fans_user_time", "user_id", "created_at"),
        Index("ix_fans_host_time", "host_user_id", "created_at"),
        Index("ix_fans_host_total", "host_user_id", "total_contribution"),
        Index("ix_fans_host_recent", "host_user_id", "last_contributed_at"),
        Index("ix_fans_status_tier", "status", "tier"),
    )
