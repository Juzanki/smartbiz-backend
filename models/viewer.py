# backend/models/viewer.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from typing import Optional, Dict, Any, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    from .live_stream import LiveStream


class Viewer(Base):
    """
    Viewer — hufuatilia uwepo & ushiriki wa mtazamaji kwenye live stream.

    - SQLAlchemy 2.0 typed mappings
    - FK sahihi kwa 'users' & 'live_streams'
    - Hybrid props: is_active, duration_seconds
    """
    __tablename__ = "viewers"
    __mapper_args__ = {"eager_defaults": True}

    # -------- Identity --------
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # -------- Foreign keys --------
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="NULL => anonymous viewer",
    )
    stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Badilisha kama jina la jedwali la stream linatofautiana.",
    )

    # -------- Presence --------
    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    left_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )

    # -------- Session info --------
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    country: Mapped[Optional[str]] = mapped_column(String(2), default=None)          # ISO-3166 alpha-2
    device_type: Mapped[Optional[str]] = mapped_column(String(32), default=None)     # Android | iPhone | Web
    app_version: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    is_muted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # -------- Engagement --------
    messages_sent: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    reactions_sent: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    total_watch_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # -------- Flexible metadata --------
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT), default=None)

    # -------- Relationships --------
    user: Mapped[Optional["User"]] = relationship(
        "User",
        lazy="selectin",
        passive_deletes=True,
        foreign_keys=[user_id],
    )
    # NOTE: tumeondoa back_populates hapa ili kuepuka mismatch na upande wa LiveStream.
    # Ukiweka upande wa LiveStream baadaye, tumia back_populates kwenye pande zote mbili.
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream",
        lazy="selectin",
        passive_deletes=True,
        foreign_keys=[stream_id],
    )

    __table_args__ = (
        # Zuia duplicates za session ile ile
        UniqueConstraint("user_id", "stream_id", "joined_at", name="uq_viewer_session_unique"),
        # Counters lazima ziwe zisizo hasi
        CheckConstraint(
            "messages_sent >= 0 AND reactions_sent >= 0 AND total_watch_seconds >= 0",
            name="ck_viewer_counts_nonneg",
        ),
        # End >= Start (au moja yao NULL)
        CheckConstraint(
            "(left_at IS NULL) OR (joined_at IS NULL) OR (left_at >= joined_at)",
            name="ck_viewer_left_after_join",
        ),
        # ISO-2 ya nchi (ikijazwa)
        CheckConstraint("country IS NULL OR length(country) = 2", name="ck_viewer_country_iso2"),
        # Query patterns za kawaida
        Index("ix_viewers_stream_time", "stream_id", "joined_at"),
        Index("ix_viewers_user_time", "user_id", "joined_at"),
        Index("ix_viewers_active", "stream_id", "left_at"),
    )

    # -------- Hybrids --------
    @hybrid_property
    def is_active(self) -> bool:
        return self.left_at is None

    @is_active.expression
    def is_active(cls):
        return cls.left_at.is_(None)

    @hybrid_property
    def duration_seconds(self) -> Optional[int]:
        """Sekunde za kikao; kama bado yupo, tumia sasa."""
        end = self.left_at or dt.datetime.now(dt.timezone.utc)
        return int((end - self.joined_at).total_seconds()) if self.joined_at else None

    @duration_seconds.expression
    def duration_seconds(cls):
        return func.cast(
            func.extract("epoch", func.coalesce(cls.left_at, func.now()) - cls.joined_at),
            Integer,
        )

    # -------- Helpers --------
    def leave(self, when: Optional[dt.datetime] = None) -> None:
        self.left_at = when or dt.datetime.now(dt.timezone.utc)

    def add_message(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("count must be >= 0")
        self.messages_sent = (self.messages_sent or 0) + count

    def add_reaction(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("count must be >= 0")
        self.reactions_sent = (self.reactions_sent or 0) + count

    def add_watch_time(self, seconds: int) -> None:
        if seconds < 0:
            raise ValueError("seconds must be >= 0")
        self.total_watch_seconds = (self.total_watch_seconds or 0) + seconds

    def session_duration(self) -> Optional[int]:
        """Rudisha muda (sekunde) kama mtazamaji ameondoka; sivyo None."""
        if self.left_at:
            return int((self.left_at - self.joined_at).total_seconds())
        return None

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Viewer id={self.id} user={self.user_id} stream={self.stream_id} "
            f"active={self.left_at is None} joined={self.joined_at}>"
        )
