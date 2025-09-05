# backend/models/video_post.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from typing import Optional, List

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    JSON,   # portable: SQLite/Postgres
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

class VideoPost(Base):
    """
    Kipande cha video (clip) kilichotokana na RecordedStream.

    - SQLAlchemy 2.0 typed mappings
    - Timezone-aware timestamps
    - Constraints + indexes
    - Soft-delete + status
    - Denormalized counters kwa feeds
    - JSON hashtags + helpers
    """
    __tablename__ = "video_posts"
    __mapper_args__ = {"eager_defaults": True}

    # -------------------- Columns --------------------
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # 1:1 na RecordedStream
    recorded_stream_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("recorded_streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Chanzo cha video: recorded_streams.id",
    )

    # Mmiliki (denormalized; si lazima ku-declare relationship kwa User hapa)
    owner_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Content
    video_url: Mapped[str] = mapped_column(String(512), nullable=False)
    caption: Mapped[Optional[str]] = mapped_column(String(500), default=None)
    hashtags: Mapped[List[str]] = mapped_column(
        JSON, default=list, nullable=False, doc='mfano: ["live","smartbiz"]'
    )
    thumbnail_url: Mapped[Optional[str]] = mapped_column(String(512), default=None)

    # Media info
    duration_sec: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    width: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    height: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    aspect_ratio: Mapped[Optional[str]] = mapped_column(String(16), default=None)  # "9:16"
    file_size_mb: Mapped[Optional[float]] = mapped_column(Numeric(12, 3), default=None)

    # Lifecycle & visibility
    is_draft: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(12), default="public", nullable=False  # public | unlisted | private
    )
    status: Mapped[str] = mapped_column(
        String(16), default="ready", nullable=False  # ready | processing | failed | blocked
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    published_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    scheduled_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )

    # Denormalized counters
    likes_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    comments_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    shares_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    views_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Hints
    storage_provider: Mapped[Optional[str]] = mapped_column(String(24), default=None)  # s3 | gcs | local
    processing_notes: Mapped[Optional[str]] = mapped_column(String(255), default=None)

    # ---------------- Relationships ----------------
    # RecordedStream upande wa pili: video_post = relationship("VideoPost", back_populates="recorded_stream", uselist=False)
    recorded_stream: Mapped["RecordedStream"] = relationship(
        "RecordedStream", back_populates="video_post", lazy="joined", uselist=False
    )

    # Highlights — upande wa pili: video_post = relationship("VideoPost", back_populates="highlights")
    highlights: Mapped[List["ReplayHighlight"]] = relationship(
        "ReplayHighlight", back_populates="video_post", cascade="all, delete-orphan", lazy="selectin"
    )

    # Matukio ya replay (likes/comments/reactions) — ReplayEvent
    # Upande wa pili (ReplayEvent): video_post = relationship("VideoPost", back_populates="replay_events")
    replay_events: Mapped[List["ReplayEvent"]] = relationship(
        "ReplayEvent",
        back_populates="video_post",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # LOGS za share/download — ReplayActivityLog
    # Upande wa pili (ReplayActivityLog): video_post = relationship("VideoPost", back_populates="replay_activity_logs")
    replay_activity_logs: Mapped[List["ReplayActivityLog"]] = relationship(
        "ReplayActivityLog",
        back_populates="video_post",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # Comments — upande wa pili: video_post = relationship("VideoPost", back_populates="comments")
    comments: Mapped[List["VideoComment"]] = relationship(
        "VideoComment", back_populates="video_post", cascade="all, delete-orphan", lazy="selectin"
    )

    # View stats — upande wa pili: video_post = relationship("VideoPost", back_populates="view_stats")
    view_stats: Mapped[List["VideoViewStat"]] = relationship(
        "VideoViewStat", back_populates="video_post", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("recorded_stream_id", name="uq_video_posts_recorded_stream"),
        CheckConstraint(
            "likes_count >= 0 AND comments_count >= 0 AND shares_count >= 0 AND views_count >= 0",
            name="ck_video_posts_counts_nonneg",
        ),
        CheckConstraint(
            "visibility in ('public','unlisted','private')",
            name="ck_video_posts_visibility_enum",
        ),
        CheckConstraint(
            "status in ('ready','processing','failed','blocked')",
            name="ck_video_posts_status_enum",
        ),
        Index("ix_video_posts_owner_published", "owner_id", "published_at"),
        Index("ix_video_posts_visibility_time", "visibility", "created_at"),
        Index("ix_video_posts_status_time", "status", "created_at"),
    )

    # ----------------- Helpers (no DB I/O) -----------------
    def publish(self, when: Optional[dt.datetime] = None) -> None:
        self.is_draft = False
        self.is_deleted = False
        self.status = "ready"
        self.published_at = when or dt.datetime.now(dt.timezone.utc)

    def unpublish(self) -> None:
        self.is_draft = True
        self.published_at = None

    def schedule(self, when: dt.datetime) -> None:
        self.scheduled_at = when

    def soft_delete(self) -> None:
        self.is_deleted = True

    def restore(self) -> None:
        self.is_deleted = False

    def update_caption(self, text: Optional[str]) -> None:
        self.caption = (text or "").strip()[:500]

    def set_hashtags(self, tags: Optional[List[str]]) -> None:
        cleaned = {(t or "").strip().lstrip("#").lower() for t in (tags or []) if (t or "").strip()}
        self.hashtags = sorted(cleaned)

    def bump_likes(self, n: int = 1) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self.likes_count += n

    def bump_comments(self, n: int = 1) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self.comments_count += n

    def bump_shares(self, n: int = 1) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self.shares_count += n

    def bump_views(self, n: int = 1) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self.views_count += n

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<VideoPost id={self.id} stream={self.recorded_stream_id} "
            f"draft={self.is_draft} vis={self.visibility} status={self.status} "
            f"likes={self.likes_count} comments={self.comments_count} views={self.views_count}>"
        )



