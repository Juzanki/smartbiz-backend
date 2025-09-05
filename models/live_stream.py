# backend/models/live_stream.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import hmac
import hashlib
import secrets
import datetime as dt
from typing import Optional, List, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.event import listens_for
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json  # portability

if TYPE_CHECKING:
    # Hizi ni kwa hints tu (hazita-import runtime)
    from .guest import Guest
    from .co_host import CoHost
    from .stream_settings import StreamSettings
    from .co_host_invite import CoHostInvite
    from .gift_fly import GiftFly
    from .gift_movement import GiftMovement
    from .gift_marker import GiftMarker
    from .viewer import Viewer              # ← IMPORTANT: tumetumia Viewer (sio LiveViewer)
    from .recorded_stream import RecordedStream
    from .post_live_notification import PostLiveNotification
    from .replay_summary import ReplaySummary
    from .replay_caption import ReplayCaption
    from .replay_title import ReplayTitle
    from .replay_analytics import ReplayAnalytics
    from .goal import Goal
    from .leaderboard_notification import LeaderboardNotification
    from .like import Like
    from .moderation import ModerationAction
    from .live_product import LiveProduct
    from .top_contributor import TopContributor


SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9\-_]+$")


class LiveStream(Base):
    """Live stream session metadata."""
    __tablename__ = "live_streams"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        # Guards
        CheckConstraint("viewers_count >= 0", name="ck_ls_viewers_nonneg"),
        CheckConstraint("likes_count   >= 0", name="ck_ls_likes_nonneg"),
        CheckConstraint(
            "ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at",
            name="ck_ls_end_after_start",
        ),
        CheckConstraint("code IS NULL OR length(code) BETWEEN 3 AND 50", name="ck_ls_code_len"),
        UniqueConstraint("code", name="uq_ls_code"),
        # Indices
        Index("ix_ls_code", "code"),
        Index("ix_ls_featured_active", "is_featured", "ended_at"),
        Index("ix_ls_started_at", "started_at"),
        Index("ix_ls_active_order", "ended_at", "started_at"),
        Index("ix_ls_created", "created_at"),
        {"extend_existing": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Public identifiers
    code: Mapped[Optional[str]] = mapped_column(String(50), unique=True, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(160))
    goal:  Mapped[Optional[str]] = mapped_column(String(160))

    # Private join/control code (hashed)
    code_hash: Mapped[Optional[str]] = mapped_column(String(128), index=True)  # hex sha256
    code_salt: Mapped[Optional[str]] = mapped_column(String(32))
    rotate_secret: Mapped[Optional[str]] = mapped_column(String(64))

    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_recorded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    viewers_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    likes_count:   Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)

    # Timestamps
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    ended_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    last_active_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted: Mapped[bool] = mapped_column(Boolean, server_default=text("0"), nullable=False, index=True)

    # ---------------- Relationships ----------------

    guests: Mapped[List["Guest"]] = relationship(
        "Guest",
        back_populates="live_stream",
        cascade="all",
        passive_deletes=True,
        lazy="selectin",
    )

    co_hosts: Mapped[List["CoHost"]] = relationship(
        "CoHost",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    settings: Mapped[Optional["StreamSettings"]] = relationship(
        "StreamSettings",
        back_populates="stream",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    cohost_invites: Mapped[List["CoHostInvite"]] = relationship(
        "CoHostInvite",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    gift_fly_events: Mapped[List["GiftFly"]] = relationship(
        "GiftFly",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    gift_movements: Mapped[List["GiftMovement"]] = relationship(
        "GiftMovement",
        primaryjoin="LiveStream.id == foreign(GiftMovement.stream_id)",
        foreign_keys="GiftMovement.stream_id",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    gift_markers: Mapped[List["GiftMarker"]] = relationship(
        "GiftMarker",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # IMPORTANT: tumia Viewer, na FK yake 'stream_id'
    viewers: Mapped[List["Viewer"]] = relationship(
        "Viewer",
        primaryjoin="LiveStream.id == foreign(Viewer.stream_id)",
        foreign_keys="Viewer.stream_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    recorded_streams: Mapped[List["RecordedStream"]] = relationship(
        "RecordedStream",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    post_live_notifications: Mapped[List["PostLiveNotification"]] = relationship(
        "PostLiveNotification",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    summary_ai: Mapped[Optional["ReplaySummary"]] = relationship(
        "ReplaySummary",
        back_populates="stream",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    captions: Mapped[List["ReplayCaption"]] = relationship(
        "ReplayCaption",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    auto_title: Mapped[Optional["ReplayTitle"]] = relationship(
        "ReplayTitle",
        back_populates="stream",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    replay_analytics: Mapped[List["ReplayAnalytics"]] = relationship(
        "ReplayAnalytics",
        back_populates="live_stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    goals: Mapped[List["Goal"]] = relationship(
        "Goal",
        back_populates="stream",
        cascade="all",
        passive_deletes=True,
        lazy="selectin",
    )

    leaderboard_notifications: Mapped[List["LeaderboardNotification"]] = relationship(
        "LeaderboardNotification",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    likes: Mapped[List["Like"]] = relationship(
        "Like",
        back_populates="live_stream",
        cascade="all",
        passive_deletes=True,
        lazy="selectin",
    )

    moderation_actions: Mapped[List["ModerationAction"]] = relationship(
        "ModerationAction",
        back_populates="live_stream",
        cascade="all",
        passive_deletes=True,
        lazy="selectin",
    )

    live_products: Mapped[List["LiveProduct"]] = relationship(
        "LiveProduct",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    top_contributors: Mapped[List["TopContributor"]] = relationship(
        "TopContributor",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # ---------------- Hybrid helpers ----------------
    @hybrid_property
    def is_live(self) -> bool:
        return self.started_at is not None and self.ended_at is None

    @is_live.expression
    def is_live(cls):
        return cls.ended_at.is_(None)

    @hybrid_property
    def duration_seconds(self) -> Optional[int]:
        if self.started_at is None:
            return None
        end = self.ended_at or dt.datetime.now(dt.timezone.utc)
        return int((end - self.started_at).total_seconds())

    @duration_seconds.expression
    def duration_seconds(cls):
        return func.cast(
            func.extract("epoch", func.coalesce(cls.ended_at, func.now()) - cls.started_at),
            Integer,
        )

    @hybrid_property
    def engagement_score(self) -> float:
        v = max(1, int(self.viewers_count or 0))
        return float((self.likes_count or 0) * 100.0 / v)

    @engagement_score.expression
    def engagement_score(cls):
        return (func.coalesce(cls.likes_count, 0) * 100.0) / func.nullif(cls.viewers_count, 0)

    @hybrid_property
    def featured_and_live(self) -> bool:
        return bool(self.is_featured and self.is_live)

    @featured_and_live.expression
    def featured_and_live(cls):
        return func.coalesce(cls.is_featured, False) & cls.ended_at.is_(None)

    # ---------------- Domain helpers ----------------
    def mark_active(self) -> None:
        self.last_active_at = dt.datetime.now(dt.timezone.utc)

    def end(self, when: Optional[dt.datetime] = None) -> None:
        self.ended_at = when or dt.datetime.now(dt.timezone.utc)

    def add_viewers(self, n: int = 1) -> None:
        self.viewers_count = max(0, (self.viewers_count or 0) + int(max(0, n)))

    def add_likes(self, n: int = 1) -> None:
        self.likes_count = max(0, (self.likes_count or 0) + int(max(0, n)))

    def feature(self, on: bool = True) -> None:
        self.is_featured = bool(on)

    def record(self, on: bool = True) -> None:
        self.is_recorded = bool(on)

    # ---- Private code (security) ----
    def set_private_code(self, raw_code: str) -> None:
        raw = (raw_code or "").strip()
        if len(raw) < 6:
            raise ValueError("Private code must be at least 6 characters.")
        salt = secrets.token_bytes(16)
        digest = hashlib.sha256(salt + raw.encode("utf-8")).hexdigest()
        self.code_salt = salt.hex()
        self.code_hash = digest

    def verify_private_code(self, candidate: str) -> bool:
        if not self.code_hash or not self.code_salt:
            return False
        salt = bytes.fromhex(self.code_salt)
        digest = hashlib.sha256(salt + (candidate or "").encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, self.code_hash)

    # ---------------- Validators & normalizers ----------------
    @validates("code")
    def _normalize_code(self, _key, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        v = value.strip()
        if not v:
            return None
        if not SAFE_CODE_RE.match(v):
            raise ValueError("code must be alphanumeric with '-' or '_' only.")
        return v

    @validates("title", "goal")
    def _trim_short_texts(self, _key, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        v = value.strip()
        return v or None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LiveStream id={self.id} code={self.code!r} title={self.title!r} live={self.is_live}>"



# ---------- Normalize before insert/update ----------
@listens_for(LiveStream, "before_insert")
def _ls_before_insert(_mapper, _conn, t: LiveStream) -> None:
    if t.code:
        t.code = t.code.strip()
    if t.title:
        t.title = t.title.strip()
    if t.goal:
        t.goal = t.goal.strip()
    if not t.rotate_secret:
        t.rotate_secret = secrets.token_hex(16)

@listens_for(LiveStream, "before_update")
def _ls_before_update(_mapper, _conn, t: LiveStream) -> None:
    if t.code:
        t.code = t.code.strip()
    if t.title:
        t.title = t.title.strip()
    if t.goal:
        t.goal = t.goal.strip()
