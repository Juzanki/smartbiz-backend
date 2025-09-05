# backend/models/leaderboard_notification.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

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
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method
from sqlalchemy.ext.mutable import MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE

if TYPE_CHECKING:
    from .user import User
    from .live_stream import LiveStream  # __tablename__="live_streams"


# -------- Enums --------
class LBType(str, enum.Enum):
    rise = "rise"
    drop = "drop"
    new = "new"
    milestone = "milestone"  # entered Top 10 / hit threshold

class LBChannel(str, enum.Enum):
    inapp = "inapp"
    push = "push"
    email = "email"
    sms = "sms"

class LBStatus(str, enum.Enum):
    created = "created"
    queued = "queued"
    delivering = "delivering"
    delivered = "delivered"
    seen = "seen"
    expired = "expired"
    failed = "failed"

class LBWindow(str, enum.Enum):
    all_time = "all_time"
    daily    = "daily"
    weekly   = "weekly"
    monthly  = "monthly"
    stream   = "stream"  # per-stream session window

class LBScope(str, enum.Enum):
    global_ = "global"
    stream  = "stream"   # tied to a stream_id

class BoardName(str, enum.Enum):
    coins  = "coins"
    gifts  = "gifts"
    points = "points"
    likes  = "likes"
    other  = "other"


class LeaderboardNotification(Base):
    """
    Arifa za mabadiliko ya rank/nafasi (global au za stream).
    • Dedupe ya mada kwa kipindi (group_key + window)
    • Uwasilishaji wenye retries, quiet-hours, na kipaumbele
    • Snapshots (position, previous_position, score_delta, percentile)
    • CTA/deeplink & i18n placeholders kupitia meta
    """
    __tablename__ = "leaderboard_notifications"
    __mapper_args__ = {"eager_defaults": True}

    # -------- Constraints & Indexes (all-in-one) --------
    __table_args__ = (
        # Ulinzi wa idempotency na dedupe ya scope
        UniqueConstraint("idempotency_key", name="uq_lb_notif_idem"),
        UniqueConstraint(
            "stream_id", "user_id", "type", "position", "request_id", "window",
            name="uq_lb_dedupe_scope",
        ),
        # Hot-path indexes
        Index("ix_lb_stream_time", "stream_id", "created_at"),
        Index("ix_lb_user_time", "user_id", "created_at"),
        Index("ix_lb_unseen_stream", "stream_id", "seen", "created_at"),
        Index("ix_lb_unseen_user", "user_id", "seen", "created_at"),
        Index("ix_lb_seen_status", "seen", "status"),
        Index("ix_lb_type_position", "type", "position"),
        Index("ix_lb_channel_status", "channel", "status"),
        Index("ix_lb_scope_window", "scope", "window"),
        Index("ix_lb_priority_queue", "status", "priority", "schedule_at"),
        Index("ix_lb_group_key", "group_key", "window"),
        # Guards
        CheckConstraint("position >= 1", name="ck_lb_pos_min"),
        CheckConstraint("(previous_position IS NULL) OR (previous_position >= 1)", name="ck_lb_prev_pos_min"),
        CheckConstraint("priority >= 0", name="ck_lb_priority_nonneg"),
        CheckConstraint(
            "(expires_at IS NULL) OR (created_at IS NULL) OR (expires_at >= created_at)",
            name="ck_lb_expiry_after_create",
        ),
        CheckConstraint("length(title) <= 160", name="ck_lb_title_len"),
        CheckConstraint("length(message) <= 280", name="ck_lb_msg_len"),
        CheckConstraint("delivery_attempts >= 0 AND max_attempts >= 0", name="ck_lb_attempts_nonneg"),
        CheckConstraint("percentile IS NULL OR (percentile BETWEEN 0 AND 100)", name="ck_lb_percentile_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # -------- Scope --------
    scope: Mapped[LBScope] = mapped_column(
        SQLEnum(LBScope, name="lb_scope", native_enum=False, validate_strings=True),
        default=LBScope.stream, nullable=False, index=True,
    )
    window: Mapped[LBWindow] = mapped_column(
        SQLEnum(LBWindow, name="lb_window", native_enum=False, validate_strings=True),
        default=LBWindow.stream, nullable=False, index=True,
    )
    board: Mapped[BoardName] = mapped_column(
        SQLEnum(BoardName, name="lb_board", native_enum=False, validate_strings=True),
        default=BoardName.coins, nullable=False, index=True,
    )

    stream_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # -------- Classification --------
    type: Mapped[LBType] = mapped_column(
        SQLEnum(LBType, name="lb_notification_type", native_enum=False, validate_strings=True),
        default=LBType.new, nullable=False, index=True,
    )
    channel: Mapped[LBChannel] = mapped_column(
        SQLEnum(LBChannel, name="lb_notification_channel", native_enum=False, validate_strings=True),
        default=LBChannel.inapp, nullable=False, index=True,
    )
    status: Mapped[LBStatus] = mapped_column(
        SQLEnum(LBStatus, name="lb_notification_status", native_enum=False, validate_strings=True),
        default=LBStatus.created, nullable=False, index=True,
    )

    # -------- Rank snapshot --------
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # rank sasa (1 bora)
    previous_position: Mapped[Optional[int]] = mapped_column(Integer)
    score_snapshot: Mapped[Optional[Decimal]] = mapped_column(DECIMAL_TYPE)
    score_delta: Mapped[Optional[Decimal]] = mapped_column(DECIMAL_TYPE)   # tofauti na snapshot ya awali
    percentile: Mapped[Optional[float]] = mapped_column(Integer)           # 0..100 (tunaweka kama int kwa portability)

    # -------- UX / delivery --------
    title:   Mapped[Optional[str]] = mapped_column(String(160))
    message: Mapped[Optional[str]] = mapped_column(String(280))
    locale:  Mapped[Optional[str]] = mapped_column(String(10), index=True)     # e.g., "en", "sw"
    deeplink_url: Mapped[Optional[str]] = mapped_column(String(512))
    seen: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # meta inatumika kuweka placeholders/variables na CTA config
    meta: Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))

    # -------- Delivery control --------
    schedule_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    quiet_hours_skip: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    last_attempt_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(String(220))

    # -------- Correlation / dedupe --------
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    request_id:      Mapped[Optional[str]] = mapped_column(String(64), index=True)
    group_key:       Mapped[Optional[str]] = mapped_column(String(120), index=True)  # e.g., "top10-entry:coins"

    # -------- Timestamps --------
    created_at:   Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    queued_at:    Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    delivered_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    seen_at:      Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    expires_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # -------- Relationships --------
    user: Mapped["User"] = relationship(
        "User",
        back_populates="leaderboard_notifications",
        foreign_keys=[user_id],
        passive_deletes=True,
        lazy="selectin",
    )
    stream: Mapped[Optional["LiveStream"]] = relationship(
        "LiveStream",
        back_populates="leaderboard_notifications",
        foreign_keys=[stream_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # -------- Hybrids --------
    @hybrid_property
    def delta(self) -> Optional[int]:
        """+ = improved/rose, - = dropped."""
        if self.previous_position is None or self.position is None:
            return None
        return int(self.previous_position) - int(self.position)

    @delta.expression
    def delta(cls):
        return cls.previous_position - cls.position

    @hybrid_property
    def moved_up(self) -> bool:
        d = self.delta
        return d is not None and d > 0

    @hybrid_property
    def moved_down(self) -> bool:
        d = self.delta
        return d is not None and d < 0

    @hybrid_property
    def is_unseen(self) -> bool:
        return (not self.seen) and (self.status != LBStatus.expired)

    @hybrid_property
    def should_deliver_now(self) -> bool:
        if self.status not in (LBStatus.created, LBStatus.queued, LBStatus.delivering):
            return False
        if self.expires_at and dt.datetime.now(dt.timezone.utc) >= self.expires_at:
            return False
        if self.schedule_at and dt.datetime.now(dt.timezone.utc) < self.schedule_at:
            return False
        return True

    # -------- Helpers --------
    def set_positions(self, *, current: int, previous: int | None) -> None:
        self.position = max(1, int(current))
        self.previous_position = int(previous) if previous is not None else None
        if previous is None:
            self.type = LBType.new
        else:
            d = self.delta or 0
            self.type = LBType.rise if d > 0 else (LBType.drop if d < 0 else LBType.milestone)

    def set_score(self, value: Decimal | int | float | str | None, *, delta: Decimal | int | float | str | None = None) -> None:
        self.score_snapshot = None if value is None else Decimal(str(value))
        self.score_delta = None if delta is None else Decimal(str(delta))

    def set_priority(self, value: int) -> None:
        self.priority = max(0, int(value))

    def mark_queued(self, *, schedule_at: dt.datetime | None = None) -> None:
        self.status = LBStatus.queued
        self.schedule_at = schedule_at or self.schedule_at or dt.datetime.now(dt.timezone.utc)

    def mark_delivering(self) -> None:
        self.status = LBStatus.delivering
        self.last_attempt_at = dt.datetime.now(dt.timezone.utc)

    def mark_delivered(self) -> None:
        self.status = LBStatus.delivered
        self.delivered_at = dt.datetime.now(dt.timezone.utc)

    def mark_failed(self, reason: str | None = None) -> None:
        self.status = LBStatus.failed
        self.last_error = (reason or "")[:220]
        self.last_attempt_at = dt.datetime.now(dt.timezone.utc)
        self.delivery_attempts = (self.delivery_attempts or 0) + 1

    def can_retry(self) -> bool:
        return self.status in (LBStatus.failed, LBStatus.queued) and (self.delivery_attempts or 0) < (self.max_attempts or 0)

    def mark_seen(self) -> None:
        self.seen = True
        self.status = LBStatus.seen
        self.seen_at = dt.datetime.now(dt.timezone.utc)

    def expire(self) -> None:
        self.status = LBStatus.expired

    def set_percentile(self, value: float | int | None) -> None:
        if value is None:
            self.percentile = None
        else:
            v = int(max(0, min(100, float(value))))
            self.percentile = v

    def set_i18n(self, *, locale: str | None = None, title: str | None = None, message: str | None = None, deeplink_url: str | None = None) -> None:
        if locale: self.locale = locale.strip()[:10]
        if title:  self.title = title[:160]
        if message: self.message = message[:280]
        if deeplink_url: self.deeplink_url = deeplink_url[:512]

    def coalesce_with(self, other: "LeaderboardNotification") -> bool:
        """
        Jaribu kuunganisha arifa mbili za mada moja (group_key + window + user + stream + board).
        Rudi True kama imeunganishwa (na hii inabaki kama record inayotumika).
        """
        same = (
            self.user_id == other.user_id and
            self.stream_id == other.stream_id and
            self.group_key and self.group_key == other.group_key and
            self.window == other.window and
            self.board == other.board
        )
        if not same:
            return False
        # chukua rank bora (position ndogo), ongeza ujumbe au weka priority kubwa
        if other.position < self.position:
            self.previous_position = self.position
            self.position = other.position
        self.priority = max(self.priority or 0, other.priority or 0)
        return True

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<LeaderboardNotification id={self.id} scope={self.scope} window={self.window} "
                f"board={self.board} stream={self.stream_id} user={self.user_id} type={self.type} "
                f"pos={self.position} prev={self.previous_position} seen={self.seen}>")

    # -------- Validators --------
    @validates("deeplink_url")
    def _v_url(self, _k: str, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = v.strip()
        return v[:512] or None

    @validates("group_key", "request_id", "idempotency_key", "locale", "title", "message")
    def _v_trim(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None
