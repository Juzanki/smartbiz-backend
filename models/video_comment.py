# backend/models/video_comment.py
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
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

class VideoComment(Base):
    """
    VideoComment — user comments on video posts (mobile-first, moderation-ready).

    Upgrades:
    - SQLAlchemy 2.0 typed mappings
    - Timezone-aware timestamps + edit tracking
    - Soft-delete & moderation status
    - Threading via parent_id (nested replies)
    - Lightweight counters (likes_count, replies_count)
    - Useful helpers: edit, soft_delete, restore, pin, set_status
    """
    __tablename__ = "video_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Ownership
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_post_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("video_posts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Threading (null = top-level)
    parent_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("video_comments.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # Content
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Moderation & visibility
    status: Mapped[str] = mapped_column(
        String(16),
        default="visible",
        nullable=False,
        doc="visible | hidden | flagged | removed",
    )
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Counters (simple denorm; update in service/CRUD)
    likes_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replies_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    edited_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="video_comments", lazy="selectin")
    video_post: Mapped["VideoPost"] = relationship("VideoPost", back_populates="comments", lazy="selectin")

    # Self-referential thread
    parent: Mapped[Optional["VideoComment"]] = relationship(
        "VideoComment", remote_side="VideoComment.id", back_populates="replies", lazy="selectin"
    )
    replies: Mapped[List["VideoComment"]] = relationship(
        "VideoComment", back_populates="parent", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("length(message) > 0", name="ck_video_comment_nonempty"),
        CheckConstraint(
            "status in ('visible','hidden','flagged','removed')",
            name="ck_video_comment_status_enum",
        ),
        CheckConstraint("likes_count >= 0 AND replies_count >= 0", name="ck_video_comment_counts_nonneg"),
        Index("ix_video_comments_post_time", "video_post_id", "created_at"),
        Index("ix_video_comments_parent_time", "parent_id", "created_at"),
        Index("ix_video_comments_user_time", "user_id", "created_at"),
    )

    # ----------------- Helpers (no DB I/O here) -----------------

    def edit(self, text: str) -> None:
        """Edit message content, keep edit timestamp."""
        new_text = (text or "").strip()
        if not new_text:
            raise ValueError("Message cannot be empty.")
        self.message = new_text
        self.edited_at = dt.datetime.now(dt.timezone.utc)

    def soft_delete(self) -> None:
        """Hide comment without dropping the row."""
        self.is_deleted = True
        self.status = "removed"

    def restore(self) -> None:
        """Restore a soft-deleted comment."""
        self.is_deleted = False
        self.status = "visible"

    def pin(self) -> None:
        self.is_pinned = True

    def unpin(self) -> None:
        self.is_pinned = False

    def set_status(self, value: str) -> None:
        value = (value or "visible").lower()
        if value not in {"visible", "hidden", "flagged", "removed"}:
            raise ValueError("Invalid status.")
        self.status = value

    def like(self, n: int = 1) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self.likes_count += n

    def unlike(self, n: int = 1) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self.likes_count = max(0, self.likes_count - n)

    def bump_replies(self, n: int = 1) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self.replies_count += n

    def __repr__(self) -> str:  # pragma: no cover
        short = (self.message[:30] + "...") if len(self.message) > 30 else self.message
        return (
            f"<VideoComment id={self.id} post={self.video_post_id} user={self.user_id} "
            f"status={self.status} pinned={self.is_pinned} deleted={self.is_deleted} msg='{short}'>"
        )



