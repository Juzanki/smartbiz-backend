# backend/models/goal_model.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING, Iterable, List

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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableList, MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    from .live_stream import LiveStream  # __tablename__ = "live_streams"


# ---------------- Enums ----------------
class GoalType(str, enum.Enum):
    likes     = "likes"
    coins     = "coins"
    gifts     = "gifts"
    followers = "followers"
    viewers   = "viewers"
    custom    = "custom"


class GoalStatus(str, enum.Enum):
    active    = "active"
    paused    = "paused"
    completed = "completed"
    expired   = "expired"
    canceled  = "canceled"


class GoalUnit(str, enum.Enum):
    count = "count"   # likes/gifts/followers/viewers
    coins = "coins"   # token/coin units
    other = "other"   # custom semantics


class Goal(Base):
    """
    Stream/creator goal: ina milestones, progress, visibility, na lifecycle helpers.
    • JSON portable (milestones/meta) na mutable (detect in-place updates)
    • Money-safe DECIMAL/NUMERIC kwa target/current
    • Helpers nyingi za kudhibiti mzunguko wa maisha (start/extend/pause/complete/expire/reset)
    """
    __tablename__ = "goals"
    __table_args__ = (
        UniqueConstraint("creator_id", "stream_id", "title", name="uq_goal_owner_stream_title"),
        Index("ix_goal_creator_created", "creator_id", "created_at"),
        Index("ix_goal_status_expires", "status", "expires_at"),
        Index("ix_goal_stream_type", "stream_id", "goal_type"),
        Index("ix_goal_active", "creator_id", "status"),
        Index("ix_goal_visibility_pin", "is_visible", "pinned"),
        CheckConstraint("target_value > 0", name="ck_goal_target_positive"),
        CheckConstraint("current_value >= 0", name="ck_goal_current_nonneg"),
        CheckConstraint("length(trim(title)) > 0", name="ck_goal_title_nonempty"),
        CheckConstraint(
            "(expires_at IS NULL) OR (started_at IS NULL) OR (expires_at >= started_at)",
            name="ck_goal_expiry_after_start",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Owner & optional scope (per stream)
    creator_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stream_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("live_streams.id", ondelete="SET NULL"),
        index=True,
    )

    # Identity / classification
    goal_type: Mapped[GoalType] = mapped_column(
        SQLEnum(GoalType, name="goal_type", native_enum=False, validate_strings=True),
        default=GoalType.custom,
        nullable=False,
        index=True,
    )
    unit: Mapped[GoalUnit] = mapped_column(
        SQLEnum(GoalUnit, name="goal_unit", native_enum=False, validate_strings=True),
        default=GoalUnit.count,
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)

    # Targets & progress
    target_value:  Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False)
    current_value: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # UX / control
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    pinned:     Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # Milestones (e.g., [10, 25, 50, 100]) + “hit” list (auto-filled)
    milestones: Mapped[Optional[list]] = mapped_column(MutableList.as_mutable(JSON_VARIANT))
    milestones_hit: Mapped[Optional[list]] = mapped_column(MutableList.as_mutable(JSON_VARIANT))

    # Extras
    meta: Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))
    auto_complete_on_reach: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    auto_expire_on_due:     Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    notify_threshold_pct:   Mapped[int]  = mapped_column(Integer, nullable=False, server_default=text("90"))  # progress ≥ this => near target
    stale_after_minutes:    Mapped[Optional[int]] = mapped_column(Integer)  # if no update for N minutes => stale

    # Lifecycle
    status: Mapped[GoalStatus] = mapped_column(
        SQLEnum(GoalStatus, name="goal_status", native_enum=False, validate_strings=True),
        default=GoalStatus.active,
        nullable=False,
        index=True,
    )
    expires_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    started_at:   Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    completed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    canceled_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, index=True)
    last_progress_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Relationships
    creator: Mapped["User"] = relationship(
        "User",
        back_populates="goals",
        foreign_keys=[creator_id],
        passive_deletes=True,
        lazy="selectin",
    )
    stream: Mapped[Optional["LiveStream"]] = relationship(
        "LiveStream",
        back_populates="goals",  # ? LiveStream.goals
        foreign_keys=[stream_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # --------------- Hybrids ---------------
    @hybrid_property
    def progress(self) -> Decimal:
        """Return 0..1 ratio (clamped)."""
        tv = self.target_value or Decimal("0")
        if tv == 0:
            return Decimal("0")
        p = (self.current_value or Decimal("0")) / tv
        return max(Decimal("0"), min(Decimal("1"), p))

    @hybrid_property
    def progress_pct(self) -> int:
        """Progress percentage (0..100)."""
        return int((self.progress * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @hybrid_property
    def remaining(self) -> Decimal:
        """Kiasi kilichobaki (≥ 0)."""
        rem = (self.target_value or Decimal("0")) - (self.current_value or Decimal("0"))
        return rem if rem > 0 else Decimal("0")

    @hybrid_property
    def time_left(self) -> Optional[int]:
        """Sekunde zilizobaki kabla ya expiry; None kama haina expiry au imeisha."""
        if not self.expires_at:
            return None
        now = dt.datetime.now(dt.timezone.utc)
        if self.expires_at <= now:
            return 0
        return int((self.expires_at - now).total_seconds())

    @hybrid_property
    def is_expired(self) -> bool:
        return bool(self.expires_at and dt.datetime.now(dt.timezone.utc) >= self.expires_at)

    @hybrid_property
    def is_terminal(self) -> bool:
        return self.status in (GoalStatus.completed, GoalStatus.expired, GoalStatus.canceled)

    @hybrid_property
    def is_active_now(self) -> bool:
        return (self.status == GoalStatus.active) and (not self.is_expired)

    @hybrid_property
    def is_nearing_target(self) -> bool:
        """True ikiwa progress >= notify_threshold_pct (kwa tahadhari/CTA)."""
        try:
            return self.progress_pct >= (self.notify_threshold_pct or 90)
        except Exception:
            return False

    @hybrid_property
    def is_stale(self) -> bool:
        """True ikiwa hakuna update kwa muda mrefu (stale_after_minutes)."""
        if not self.stale_after_minutes or not self.last_progress_at:
            return False
        now = dt.datetime.now(dt.timezone.utc)
        delta = now - self.last_progress_at
        return delta.total_seconds() >= (self.stale_after_minutes * 60)

    # --------------- Internals ---------------
    @staticmethod
    def _q2(val: Decimal | int | float | str | None) -> Decimal:
        if val is None:
            return Decimal("0.00")
        d = val if isinstance(val, Decimal) else Decimal(str(val))
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # --------------- Validators ---------------
    @validates("title")
    def _v_title(self, _k: str, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("title is required")
        return v[:120]

    @validates("notify_threshold_pct")
    def _v_threshold(self, _k: str, v: int) -> int:
        iv = int(v or 0)
        return max(0, min(100, iv))

    # --------------- Mutators / Lifecycle ---------------
    def set_target(self, value: Decimal | int | float | str) -> None:
        self.target_value = self._q2(value)

    def set_progress(self, value: Decimal | int | float | str) -> None:
        self.current_value = self._q2(value)
        self.last_progress_at = dt.datetime.now(dt.timezone.utc)
        self._post_progress()

    def increment(self, amount: Decimal | int | float | str = 1) -> Decimal:
        inc = self._q2(amount)
        self.current_value = self._q2((self.current_value or Decimal("0")) + inc)
        self.last_progress_at = dt.datetime.now(dt.timezone.utc)
        self._post_progress()
        return self.current_value or Decimal("0")

    def reset_progress(self) -> None:
        self.current_value = self._q2(0)
        self.milestones_hit = []
        self.last_progress_at = dt.datetime.now(dt.timezone.utc)
        if self.is_terminal:
            # usibadilishe status za completed/expired/canceled bila makusudi
            return

    def start_now(self) -> None:
        """Iwapo goal ilitengenezwa mapema lakini haikuanzia rasmi."""
        self.started_at = dt.datetime.now(dt.timezone.utc)
        if self.status == GoalStatus.paused:
            self.status = GoalStatus.active

    def extend(self, *, seconds: int | None = None, minutes: int | None = None, hours: int | None = None) -> None:
        if not self.expires_at:
            return
        delta = dt.timedelta(seconds=seconds or 0, minutes=minutes or 0, hours=hours or 0)
        self.expires_at = self.expires_at + delta

    def restart(self, *, keep_target: bool = True) -> None:
        """Anza upya: weka current=0, hifadhi target kama keep_target=True, re-activate."""
        if not keep_target:
            self.target_value = self._q2(0)
        self.reset_progress()
        self.status = GoalStatus.active
        self.completed_at = None
        self.canceled_at = None
        self.started_at = dt.datetime.now(dt.timezone.utc)

    def pause(self) -> None:
        if not self.is_terminal:
            self.status = GoalStatus.paused

    def resume(self) -> None:
        if self.status == GoalStatus.paused and not self.is_expired:
            self.status = GoalStatus.active

    def cancel(self) -> None:
        self.status = GoalStatus.canceled
        self.canceled_at = dt.datetime.now(dt.timezone.utc)

    def complete_now(self) -> None:
        self.status = GoalStatus.completed
        self.completed_at = dt.datetime.now(dt.timezone.utc)

    def expire_now(self) -> None:
        self.status = GoalStatus.expired

    # --------------- Milestones ---------------
    def set_milestones(self, items: Iterable[Decimal | int | float | str]) -> None:
        vals = [self._q2(x) for x in (items or [])]
        vals = sorted({v for v in vals if v > 0})
        self.milestones = vals
        if self.milestones_hit is None:
            self.milestones_hit = []

    def check_milestones(self) -> list[Decimal]:
        """
        Linganisha progress ya sasa dhidi ya milestones.
        Rudisha list ya milestones mpya zilizofikiwa (na kuzihifadhi kwenye milestones_hit).
        """
        if not self.milestones:
            return []
        hit = set(self.milestones_hit or [])
        now = self.current_value or Decimal("0")
        new_hits = [m for m in self.milestones if m <= now and m not in hit]
        if new_hits:
            self.milestones_hit = sorted([*hit, *new_hits], key=Decimal)
        return new_hits

    # --------------- Internals ---------------
    def _post_progress(self) -> None:
        # 1) milestones
        self.check_milestones()
        # 2) auto-complete
        if self.auto_complete_on_reach and not self.is_terminal and self.current_value >= self.target_value:
            self.status = GoalStatus.completed
            self.completed_at = dt.datetime.now(dt.timezone.utc)
        # 3) auto-expire
        if self.auto_expire_on_due and self.is_expired and not self.is_terminal:
            self.status = GoalStatus.expired

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<Goal id={self.id} creator={self.creator_id} type={self.goal_type} "
                f"title={self.title!r} {self.current_value}/{self.target_value} "
                f"progress={self.progress_pct}% status={self.status}>")
