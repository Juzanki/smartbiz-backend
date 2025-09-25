# backend/models/live_stream.py
# -*- coding: utf-8 -*-
"""
LiveStream model — metadata na uhusiano wa live session.

- Imports salama (backend.db → db) kuzuia migongano ya packages.
- Relationships thabiti + back_populates na foreign_keys zilizo wazi.
- Hybrid properties (is_live, duration_seconds, engagement_score).
- Helpers: start(), end(), feature(), record(), add_viewers/likes(),
  mark_active(), ensure_code(), set_private_code(), verify_private_code(),
  end_and_deactivate_viewers(), recompute_counters() n.k.
- Query helpers: by_code(), live_featured().

NB: Epuka ku-import models package kwa njia mbili tofauti. Tumia canonical
'backend.models' (au alias yake 'models') tu kwenye mradi mzima.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import re
import secrets
from typing import List, Optional, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
    update,
    text,
)
from sqlalchemy.event import listens_for
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates, Session

# ── Layout-safe Base import (USI-import njia zote mbili kwingineko) ───────────
try:  # preferred canonical path
    from backend.db import Base  # type: ignore
except Exception:  # fallback if project runs without the 'backend.' package
    from db import Base  # type: ignore

if TYPE_CHECKING:
    # Hizi ni kwa type hints tu. Hakuna runtime import hapa — hakuna circular import.
    from .guest import Guest
    from .co_host import CoHost
    from .stream_settings import StreamSettings
    from .co_host_invite import CoHostInvite
    from .gift_fly import GiftFly
    from .gift_movement import GiftMovement
    from .gift_marker import GiftMarker
    from .viewer import Viewer
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

__all__ = ["LiveStream", "LiveSession"]

SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9\-_]+$")


class LiveStream(Base):
    """Live stream session metadata."""
    __tablename__ = "live_streams"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        CheckConstraint("viewers_count >= 0", name="ck_ls_viewers_nonneg"),
        CheckConstraint("likes_count   >= 0", name="ck_ls_likes_nonneg"),
        CheckConstraint(
            "ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at",
            name="ck_ls_end_after_start",
        ),
        CheckConstraint("code IS NULL OR length(code) BETWEEN 3 AND 50", name="ck_ls_code_len"),
        UniqueConstraint("code", name="uq_ls_code"),
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
    goal: Mapped[Optional[str]] = mapped_column(String(160))

    # Private join/control code (hashed + salted per-row)
    code_hash: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    code_salt: Mapped[Optional[str]] = mapped_column(String(32))
    rotate_secret: Mapped[Optional[str]] = mapped_column(String(64))

    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_recorded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    viewers_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    likes_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)

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

    # ────────────────────────────── Relationships ──────────────────────────────
    guests: Mapped[List["Guest"]] = relationship(
        "Guest",
        primaryjoin="LiveStream.id == foreign(Guest.live_stream_id)",
        foreign_keys="Guest.live_stream_id",
        back_populates="live_stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    co_hosts: Mapped[List["CoHost"]] = relationship(
        "CoHost",
        primaryjoin="LiveStream.id == foreign(CoHost.stream_id)",
        foreign_keys="CoHost.stream_id",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    settings: Mapped[Optional["StreamSettings"]] = relationship(
        "StreamSettings",
        primaryjoin="LiveStream.id == foreign(StreamSettings.stream_id)",
        foreign_keys="StreamSettings.stream_id",
        back_populates="stream",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    cohost_invites: Mapped[List["CoHostInvite"]] = relationship(
        "CoHostInvite",
        primaryjoin="LiveStream.id == foreign(CoHostInvite.stream_id)",
        foreign_keys="CoHostInvite.stream_id",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    gift_fly_events: Mapped[List["GiftFly"]] = relationship(
        "GiftFly",
        primaryjoin="LiveStream.id == foreign(GiftFly.stream_id)",
        foreign_keys="GiftFly.stream_id",
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
        primaryjoin="LiveStream.id == foreign(GiftMarker.stream_id)",
        foreign_keys="GiftMarker.stream_id",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    viewers: Mapped[List["Viewer"]] = relationship(
        "Viewer",
        primaryjoin="LiveStream.id == foreign(Viewer.stream_id)",
        foreign_keys="Viewer.stream_id",
        back_populates="live_stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    recorded_streams: Mapped[List["RecordedStream"]] = relationship(
        "RecordedStream",
        primaryjoin="LiveStream.id == foreign(RecordedStream.stream_id)",
        foreign_keys="RecordedStream.stream_id",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    post_live_notifications: Mapped[List["PostLiveNotification"]] = relationship(
        "PostLiveNotification",
        primaryjoin="LiveStream.id == foreign(PostLiveNotification.stream_id)",
        foreign_keys="PostLiveNotification.stream_id",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    summary_ai: Mapped[Optional["ReplaySummary"]] = relationship(
        "ReplaySummary",
        primaryjoin="LiveStream.id == foreign(ReplaySummary.stream_id)",
        foreign_keys="ReplaySummary.stream_id",
        back_populates="stream",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    captions: Mapped[List["ReplayCaption"]] = relationship(
        "ReplayCaption",
        primaryjoin="LiveStream.id == foreign(ReplayCaption.stream_id)",
        foreign_keys="ReplayCaption.stream_id",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    auto_title: Mapped[Optional["ReplayTitle"]] = relationship(
        "ReplayTitle",
        primaryjoin="LiveStream.id == foreign(ReplayTitle.stream_id)",
        foreign_keys="ReplayTitle.stream_id",
        back_populates="stream",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    replay_analytics: Mapped[List["ReplayAnalytics"]] = relationship(
        "ReplayAnalytics",
        primaryjoin="LiveStream.id == foreign(ReplayAnalytics.stream_id)",
        foreign_keys="ReplayAnalytics.stream_id",
        back_populates="live_stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    goals: Mapped[List["Goal"]] = relationship(
        "Goal",
        primaryjoin="LiveStream.id == foreign(Goal.stream_id)",
        foreign_keys="Goal.stream_id",
        back_populates="stream",
        cascade="all, delete",
        passive_deletes=True,
        lazy="selectin",
    )

    leaderboard_notifications: Mapped[List["LeaderboardNotification"]] = relationship(
        "LeaderboardNotification",
        primaryjoin="LiveStream.id == foreign(LeaderboardNotification.stream_id)",
        foreign_keys="LeaderboardNotification.stream_id",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    likes: Mapped[List["Like"]] = relationship(
        "Like",
        primaryjoin="LiveStream.id == foreign(Like.live_stream_id)",
        foreign_keys="Like.live_stream_id",
        back_populates="live_stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    moderation_actions: Mapped[List["ModerationAction"]] = relationship(
        "ModerationAction",
        primaryjoin="LiveStream.id == foreign(ModerationAction.live_stream_id)",
        foreign_keys="ModerationAction.live_stream_id",
        back_populates="live_stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    live_products: Mapped[List["LiveProduct"]] = relationship(
        "LiveProduct",
        primaryjoin="LiveStream.id == foreign(LiveProduct.stream_id)",
        foreign_keys="LiveProduct.stream_id",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    top_contributors: Mapped[List["TopContributor"]] = relationship(
        "TopContributor",
        primaryjoin="LiveStream.id == foreign(TopContributor.stream_id)",
        foreign_keys="TopContributor.stream_id",
        back_populates="stream",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # ────────────────────────────── Hybrid helpers ──────────────────────────────
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

    # ────────────────────────────── Domain helpers ──────────────────────────────
    def start(self) -> None:
        """Anzisha live (kama tayari live, haibadilishi started_at)."""
        if not self.started_at:
            self.started_at = dt.datetime.now(dt.timezone.utc)
        self.ended_at = None
        self.mark_active()

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
    def ensure_code(self, *, length: int = 8) -> None:
        """Hakikisha `code` ya umma ipo (alfanumeriki + -/_)."""
        if self.code:
            return
        rng = secrets.token_urlsafe(length + 2).replace("=", "")
        # safisha kwa SAFE_CODE_RE
        cleaned = "".join(ch for ch in rng if SAFE_CODE_RE.match(ch))
        self.code = cleaned[: max(3, min(50, length))]

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

    # ---- Counter recompute & bulk operations (lazy imports to avoid cycles) ---
    def end_and_deactivate_viewers(self, session: Session) -> int:
        """
        Mmalize stream na uwaweke inactive watazamaji waliopo (active=true).
        Inarudisha idadi ya rows zilizoguswa.
        """
        try:
            from backend.models.viewer import Viewer  # lazy canonical import
        except Exception:  # pragma: no cover
            from models.viewer import Viewer  # type: ignore

        self.end()
        res = session.execute(
            update(Viewer)
            .where(Viewer.stream_id == self.id, Viewer.is_active.is_(True))
            .values(is_active=False, left_at=func.coalesce(Viewer.last_seen_at, func.now()))
        )
        return int(res.rowcount or 0)

    def recompute_counters(self, session: Session) -> None:
        """
        Hesabu upya viewers_count/likes_count kutoka kwenye tables husika.
        Hii ni ya usahihi wa takwimu (si lazima kuitwa kila request).
        """
        try:
            from backend.models.viewer import Viewer  # lazy
        except Exception:  # pragma: no cover
            from models.viewer import Viewer  # type: ignore

        likes_ct = None
        try:
            from backend.models.like import Like  # type: ignore
        except Exception:  # pragma: no cover
            try:
                from models.like import Like  # type: ignore
            except Exception:
                Like = None  # type: ignore

        viewers_ct = session.execute(
            select(func.count()).select_from(Viewer).where(Viewer.stream_id == self.id)
        ).scalar_one()

        if Like is not None:  # type: ignore
            likes_ct = session.execute(
                select(func.count()).select_from(Like).where(Like.live_stream_id == self.id)
            ).scalar_one()

        self.viewers_count = int(viewers_ct or 0)
        if likes_ct is not None:
            self.likes_count = int(likes_ct or 0)

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
        if len(v) < 3 or len(v) > 50:
            raise ValueError("code length must be between 3 and 50.")
        return v

    @validates("title", "goal")
    def _trim_short_texts(self, _key, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        v = value.strip()
        return v or None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LiveStream id={self.id} code={self.code!r} title={self.title!r} live={self.is_live}>"

    # ---------------- Query helpers (class methods) ----------------
    @classmethod
    def by_code(cls, session: Session, code: str) -> Optional["LiveStream"]:
        return session.execute(select(cls).where(cls.code == (code or "").strip()).limit(1)).scalar_one_or_none()

    @classmethod
    def live_featured(cls, session: Session) -> List["LiveStream"]:
        return list(session.execute(select(cls).where(cls.is_featured.is_(True), cls.ended_at.is_(None))).scalars())


# ---------- Normalize before insert/update ----------
@listens_for(LiveStream, "before_insert")
def _ls_before_insert(_mapper, _conn, t: LiveStream) -> None:
    for attr in ("code", "title", "goal"):
        v = getattr(t, attr, None)
        if v:
            setattr(t, attr, v.strip())
    if not t.rotate_secret:
        t.rotate_secret = secrets.token_hex(16)

@listens_for(LiveStream, "before_update")
def _ls_before_update(_mapper, _conn, t: LiveStream) -> None:
    for attr in ("code", "title", "goal"):
        v = getattr(t, attr, None)
        if v:
            setattr(t, attr, v.strip())


# ── Back-compat alias (kama kulikuwa na jina la zamani kwenye code) ───────────
LiveSession = LiveStream
