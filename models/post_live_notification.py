# backend/models/post_live_notification.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import enum
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
    Text,
    UniqueConstraint,
    func,
    text,  # ✅ muhimu kwa server_default=text("...")
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    from .live_stream import LiveStream

# --------- Enums ---------
class LiveNotifKind(str, enum.Enum):
    go_live   = "go_live"
    upcoming  = "upcoming"
    ended     = "ended"
    highlight = "highlight"

class LiveNotifStatus(str, enum.Enum):
    created   = "created"
    queued    = "queued"
    delivered = "delivered"
    seen      = "seen"
    read      = "read"
    failed    = "failed"
    expired   = "expired"

class LiveNotifPriority(str, enum.Enum):
    low    = "low"
    normal = "normal"
    high   = "high"
    urgent = "urgent"

class LiveNotifChannel(str, enum.Enum):
    inapp   = "inapp"
    push    = "push"
    email   = "email"
    sms     = "sms"
    webhook = "webhook"

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class PostLiveNotification(Base):
    """
    Arifa maalum kwa LiveStream:
      - Dedupe per (user_id, stream_id, kind) + collapse_key/priority/channel
      - Lifecycle (created→queued→delivered/seen/read/failed/expired)
      - Scheduling (scheduled_at / deliver_after / next_attempt_at) + retries
      - Idempotency & request correlation
      - Meta/data (mutable JSON) kwa deep-linking/CTA
    """
    __tablename__ = "post_live_notifications"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Walengwa & chanzo
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user: Mapped["User"] = relationship(
        "User", back_populates="post_live_notifications",
        passive_deletes=True, lazy="selectin",
    )
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream", back_populates="post_live_notifications",
        passive_deletes=True, lazy="selectin",
    )

    # Uainishaji
    kind: Mapped[LiveNotifKind] = mapped_column(
        SQLEnum(LiveNotifKind, name="live_notif_kind", native_enum=False, validate_strings=True),
        default=LiveNotifKind.go_live, nullable=False, index=True,
    )
    status: Mapped[LiveNotifStatus] = mapped_column(
        SQLEnum(LiveNotifStatus, name="live_notif_status", native_enum=False, validate_strings=True),
        default=LiveNotifStatus.created, nullable=False, index=True,
    )
    priority: Mapped[LiveNotifPriority] = mapped_column(
        SQLEnum(LiveNotifPriority, name="live_notif_priority", native_enum=False, validate_strings=True),
        default=LiveNotifPriority.normal, nullable=False, index=True,
    )
    channel: Mapped[LiveNotifChannel] = mapped_column(
        SQLEnum(LiveNotifChannel, name="live_notif_channel", native_enum=False, validate_strings=True),
        default=LiveNotifChannel.inapp, nullable=False, index=True,
    )

    # Maudhui
    title:    Mapped[Optional[str]] = mapped_column(String(160))
    message:  Mapped[Optional[str]] = mapped_column(Text)
    deep_link: Mapped[Optional[str]] = mapped_column(String(512))  # smartbiz://live/123?...
    data:     Mapped[Optional[dict]] = mapped_column(as_mutable_json(JSON_VARIANT))
    meta:     Mapped[Optional[dict]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Collapse/dedupe ya tukio moja
    collapse_key:   Mapped[Optional[str]] = mapped_column(String(120), index=True)  # "live:123/go_live"
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    request_id:      Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # Flags & lifecycle
    is_read:   Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_silent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)  # no sound/badge

    # Scheduling & retries
    scheduled_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    deliver_after:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    next_attempt_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    retry_count:     Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))   # ✅
    max_retries:     Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))   # ✅

    delivered_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    seen_at:      Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    read_at:      Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    failed_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    expires_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Errors
    error_code:   Mapped[Optional[str]] = mapped_column(String(80), index=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text)

    # Audit
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ---------- Indexes / Guards ----------
    __table_args__ = (
        # Uniqueness/dedupe
        UniqueConstraint("user_id", "stream_id", "kind", name="uq_pln_user_stream_kind"),
        # Hot-path indexes
        Index("ix_pln_user_created", "user_id", "created_at"),
        Index("ix_pln_stream_created", "stream_id", "created_at"),
        Index("ix_pln_status_time", "status", "created_at"),
        Index("ix_pln_priority_channel", "priority", "channel"),
        Index("ix_pln_is_read", "is_read"),
        Index("ix_pln_scheduling", "scheduled_at", "next_attempt_at"),
        # Guards
        CheckConstraint("title IS NULL OR length(title) >= 2", name="ck_pln_title_len"),
        CheckConstraint("message IS NULL OR length(message) >= 1", name="ck_pln_message_len"),
        CheckConstraint("retry_count >= 0", name="ck_pln_retry_nonneg"),
        CheckConstraint("max_retries >= 0", name="ck_pln_max_retries_nonneg"),
        CheckConstraint(
            "(expires_at IS NULL) OR (created_at IS NULL) OR (expires_at >= created_at)",
            name="ck_pln_expiry_after_create",
        ),
        CheckConstraint(
            "(scheduled_at IS NULL) OR (scheduled_at >= created_at)",
            name="ck_pln_sched_after_create",
        ),
        CheckConstraint(
            "(next_attempt_at IS NULL) OR (next_attempt_at >= created_at)",
            name="ck_pln_next_after_create",
        ),
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def is_active(self) -> bool:
        if self.status in (LiveNotifStatus.failed, LiveNotifStatus.expired, LiveNotifStatus.read):
            return False
        if self.expires_at and _utcnow() >= self.expires_at:
            return False
        return True

    @hybrid_property
    def is_critical(self) -> bool:
        return self.priority in (LiveNotifPriority.high, LiveNotifPriority.urgent)

    @hybrid_property
    def can_attempt_send(self) -> bool:
        """Ready for dispatcher to try a delivery attempt now?"""
        if not self.is_active:
            return False
        if self.status not in (LiveNotifStatus.created, LiveNotifStatus.queued, LiveNotifStatus.failed):
            return False
        if self.deliver_after and _utcnow() < self.deliver_after:
            return False
        if self.next_attempt_at and _utcnow() < self.next_attempt_at:
            return False
        return True

    # ---------- Validators ----------
    @validates("title", "message", "deep_link", "collapse_key", "idempotency_key", "request_id", "error_code")
    def _trim_texts(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    # ---------- Helpers ----------
    def queue(self, *, when: Optional[dt.datetime] = None) -> None:
        self.status = LiveNotifStatus.queued
        self.scheduled_at = when
        self.next_attempt_at = when or _utcnow()

    def schedule_for(self, when: dt.datetime) -> None:
        self.status = LiveNotifStatus.queued
        self.scheduled_at = when
        self.deliver_after = when
        self.next_attempt_at = when

    def snooze(self, *, minutes: int = 10) -> None:
        nxt = _utcnow() + dt.timedelta(minutes=max(1, minutes))
        self.status = LiveNotifStatus.queued
        self.next_attempt_at = nxt
        self.deliver_after = min(self.deliver_after, nxt) if self.deliver_after else nxt

    def backoff_retry(self, *, base_seconds: int = 30) -> None:
        """Exponential backoff: min(30s, …) → doubles per retry; cap ~1h."""
        self.retry_count = (self.retry_count or 0) + 1
        delay = min(3600, max(base_seconds, base_seconds * (2 ** (self.retry_count - 1))))
        self.next_attempt_at = _utcnow() + dt.timedelta(seconds=delay)
        self.status = LiveNotifStatus.failed if self.retry_count >= self.max_retries else LiveNotifStatus.queued

    def mark_delivered(self) -> None:
        self.status = LiveNotifStatus.delivered
        self.delivered_at = _utcnow()
        self.error_code = None
        self.error_detail = None

    def mark_seen(self) -> None:
        self.status = LiveNotifStatus.seen
        self.seen_at = _utcnow()

    def mark_read(self) -> None:
        self.is_read = True
        self.status = LiveNotifStatus.read
        self.read_at = _utcnow()

    def fail_once(self, *, code: Optional[str] = None, detail: Optional[str] = None) -> None:
        self.error_code = code
        self.error_detail = detail
        self.backoff_retry()

    def expire(self) -> None:
        self.status = LiveNotifStatus.expired

    def should_collapse_with(self, other: "PostLiveNotification") -> bool:
        """Arifa mbili zinaweza kuunganishwa? (kupunguza kelele)."""
        if self.user_id != other.user_id or self.stream_id != other.stream_id:
            return False
        if self.collapse_key and other.collapse_key:
            return self.collapse_key == other.collapse_key
        return self.kind == other.kind and self.channel == other.channel and self.priority == other.priority

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<PostLiveNotification id={self.id} user={self.user_id} stream={self.stream_id} "
            f"kind={self.kind} status={self.status} prio={self.priority} ch={self.channel}>"
        )
