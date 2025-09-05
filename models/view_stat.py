# backend/models/video_view_stat.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import datetime as dt
from typing import Optional, Dict, Any

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer,
    Numeric, String, JSON, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

_PCT = Numeric(5, 2)  # 0.00 .. 100.00

class VideoViewStat(Base):
    """
    Tukio la kila view la video (mobile-first, analytics-ready).
    - Viewer anaweza kuwa anonymous (viewer_id=NULL)
    - Muktadha wa acquisition (source, referer, A/B)
    - Sifa za playback + engagement flags
    - Buckets za siku kwa trending/uniques
    """
    __tablename__ = "video_view_stats"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Kiungo cha msingi
    video_post_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("video_posts.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    viewer_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True, doc="NULL => anonymous viewer"
    )

    # Session / kifaa
    session_id: Mapped[Optional[str]] = mapped_column(String(64), default=None, index=True)
    ip_hash: Mapped[Optional[str]] = mapped_column(String(64), default=None, index=True)
    device_type: Mapped[Optional[str]] = mapped_column(String(32), default=None)  # Android | iPhone | Web | Desktop
    os_version: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    app_version: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    ua_hash: Mapped[Optional[str]] = mapped_column(String(64), default=None, index=True)

    # Acquisition
    source: Mapped[str] = mapped_column(String(16), default="feed", nullable=False)
    referer: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    ab_variant: Mapped[Optional[str]] = mapped_column(String(24), default=None)

    # Playback
    is_autoplay: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_muted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    quality: Mapped[str] = mapped_column(String(8), default="auto", nullable=False)  # auto|sd|hd|fhd|uhd
    watched_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    video_duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    completion_pct: Mapped[Optional[float]] = mapped_column(_PCT, default=None)
    stop_reason: Mapped[Optional[str]] = mapped_column(String(16), default=None)   # completed|skipped|paused|error|unknown
    error_code: Mapped[Optional[str]] = mapped_column(String(32), default=None)

    # Engagement
    liked_after_view: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    commented_after_view: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    shared_after_view: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Buckets / dedupe
    day_bucket: Mapped[dt.date] = mapped_column(Date, server_default=func.current_date(), nullable=False, index=True)
    is_unique_view: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Timestamps
    viewed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Metadata ya ziada
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)

    # Relationships
    video_post: Mapped["VideoPost"] = relationship("VideoPost", back_populates="view_stats", lazy="selectin")
    viewer: Mapped[Optional["User"]] = relationship(
        "User", back_populates="view_stats", foreign_keys="VideoViewStat.viewer_user_id", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("video_post_id", "viewer_user_id", "day_bucket", name="uq_view_unique_by_viewer_per_day"),
        CheckConstraint("watched_seconds >= 0", name="ck_view_watched_seconds_nonneg"),
        CheckConstraint("completion_pct IS NULL OR (completion_pct >= 0 AND completion_pct <= 100)", name="ck_view_completion_pct_bounds"),
        CheckConstraint("source in ('feed','profile','search','share','external','other')", name="ck_view_source_enum"),
        CheckConstraint("quality in ('auto','sd','hd','fhd','uhd')", name="ck_view_quality_enum"),
        CheckConstraint("stop_reason IS NULL OR stop_reason in ('completed','skipped','paused','error','unknown')", name="ck_view_stop_reason_enum"),
        Index("ix_view_post_time", "video_post_id", "viewed_at"),
        Index("ix_view_viewer_time", "viewer_user_id", "viewed_at"),
        Index("ix_view_source_time", "source", "viewed_at"),
        Index("ix_view_post_day", "video_post_id", "day_bucket"),
    )

    # Helpers
    @staticmethod
    def _sha256(text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def set_ip_hash(self, ip: Optional[str]) -> None:
        self.ip_hash = self._sha256(ip)

    def set_ua_hash(self, ua: Optional[str]) -> None:
        self.ua_hash = self._sha256(ua)

    def set_progress(self, watched: int, duration: Optional[int]) -> None:
        watched = max(0, watched)
        self.watched_seconds = watched
        self.video_duration_seconds = duration or self.video_duration_seconds
        if duration and duration > 0:
            pct = round((watched / float(duration)) * 100.0, 2)
            self.completion_pct = max(0.0, min(100.0, pct))
        else:
            self.completion_pct = None

    def mark_engagement(self, *, liked: bool = False, commented: bool = False, shared: bool = False) -> None:
        if liked: self.liked_after_view = True
        if commented: self.commented_after_view = True
        if shared: self.shared_after_view = True

    def mark_unique(self) -> None:
        self.is_unique_view = True

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<VideoViewStat id={self.id} post={self.video_post_id} viewer={self.viewer_id} "
                f"watched={self.watched_seconds}s pct={self.completion_pct} day={self.day_bucket} src={self.source}>")


