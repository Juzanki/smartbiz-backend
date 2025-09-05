# backend/models/live_session.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from decimal import Decimal
from typing import Optional, TYPE_CHECKING, List

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

if TYPE_CHECKING:
    from .user import User


# ---------- Enums ----------
class LiveStatus(str, enum.Enum):
    scheduled = "scheduled"
    live = "live"
    ended = "ended"
    canceled = "canceled"


class Visibility(str, enum.Enum):
    public = "public"
    unlisted = "unlisted"
    private = "private"


class StreamPlatform(str, enum.Enum):
    internal = "internal"
    youtube = "youtube"
    tiktok = "tiktok"
    instagram = "instagram"
    facebook = "facebook"
    twitch = "twitch"
    other = "other"


# ---------- Model ----------
class LiveSession(Base):
    """
    LiveSession — kikao cha matangazo ya moja kwa moja kinachomilikiwa na mtumiaji.
    Hutunza metadata, uonekano, takwimu, na helpers za lifecycle.
    """
    __tablename__ = "live_sessions"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("room_id", name="uq_live_session_room"),
        Index("ix_ls_user_created", "user_id", "created_at"),
        Index("ix_ls_status_started", "status", "started_at"),
        Index("ix_ls_visibility_active", "visibility", "active"),
        Index("ix_ls_platform", "platform"),
        # Guards
        CheckConstraint("length(title) >= 2", name="ck_ls_title_len"),
        CheckConstraint("viewer_count >= 0 AND peak_viewers >= 0", name="ck_ls_viewers_nonneg"),
        CheckConstraint("like_count >= 0 AND gift_count >= 0", name="ck_ls_counts_nonneg"),
        CheckConstraint("coins_earned >= 0", name="ck_ls_coins_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Owner
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user: Mapped["User"] = relationship(
        "User",
        back_populates="live_sessions",
        passive_deletes=True,
        lazy="selectin",
    )

    # Identity / scope
    title: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    tags: Mapped[Optional[List[str]]] = mapped_column(as_mutable_json(JSON_VARIANT))  # ["sale","electronics"]
    room_id: Mapped[Optional[str]] = mapped_column(String(120), index=True)  # chumba/RTC id
    platform: Mapped[StreamPlatform] = mapped_column(
        SQLEnum(StreamPlatform, name="live_session_platform"),
        default=StreamPlatform.internal,
        nullable=False,
        index=True,
    )

    # Visibility & status
    visibility: Mapped[Visibility] = mapped_column(
        SQLEnum(Visibility, name="live_session_visibility"),
        default=Visibility.public,
        nullable=False,
        index=True,
    )
    status: Mapped[LiveStatus] = mapped_column(
        SQLEnum(LiveStatus, name="live_session_status"),
        default=LiveStatus.scheduled,
        nullable=False,
        index=True,
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    # Products (JSON list[int] for portability)
    selected_products: Mapped[Optional[List[int]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Media
    cover_url: Mapped[Optional[str]] = mapped_column(String(512))
    thumbnail_url: Mapped[Optional[str]] = mapped_column(String(512))

    # Stats
    viewer_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    peak_viewers: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    like_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    gift_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    coins_earned: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # Timestamps (timezone-aware)
    scheduled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    # ----- Hybrids -----
    @hybrid_property
    def is_live(self) -> bool:
        return self.status == LiveStatus.live and self.started_at is not None and self.ended_at is None

    @hybrid_property
    def is_over(self) -> bool:
        return self.status in (LiveStatus.ended, LiveStatus.canceled) or self.ended_at is not None

    @hybrid_property
    def duration_seconds(self) -> Optional[int]:
        if self.started_at is None:
            return None
        end = self.ended_at or dt.datetime.now(dt.timezone.utc)
        return int((end - self.started_at).total_seconds())

    # ----- Helpers -----
    def go_live(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        self.status = LiveStatus.live
        self.active = True
        self.started_at = self.started_at or now

    def end(self) -> None:
        if self.ended_at is None:
            self.ended_at = dt.datetime.now(dt.timezone.utc)
        self.status = LiveStatus.ended
        self.active = False

    def cancel(self) -> None:
        self.status = LiveStatus.canceled
        self.active = False
        if self.started_at and not self.ended_at:
            self.ended_at = dt.datetime.now(dt.timezone.utc)

    def bump_viewers(self, n: int = 1) -> None:
        self.viewer_count = max(0, (self.viewer_count or 0) + max(0, int(n)))
        self.peak_viewers = max(self.peak_viewers or 0, self.viewer_count or 0)

    def add_likes(self, n: int = 1) -> None:
        self.like_count = max(0, (self.like_count or 0) + max(0, int(n)))

    def add_gifts(self, count: int = 1, coins: Decimal | int | float | str = 0) -> None:
        self.gift_count = max(0, (self.gift_count or 0) + max(0, int(count)))
        inc = coins if isinstance(coins, Decimal) else Decimal(str(coins))
        self.coins_earned = (self.coins_earned or Decimal("0")) + max(Decimal("0"), inc)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<LiveSession id={self.id} user={self.user_id} "
            f"title={self.title!r} status={self.status} live={self.is_live}>"
        )
