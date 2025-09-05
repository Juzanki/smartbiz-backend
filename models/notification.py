# backend/models/notification.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import re
import datetime as dt
from typing import Optional, TYPE_CHECKING, Dict, Any, List

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

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

if TYPE_CHECKING:
    from .user import User


# --------- Enums ---------
class NotificationType(str, enum.Enum):
    system  = "system"
    message = "message"
    billing = "billing"
    alert   = "alert"
    order   = "order"
    social  = "social"
    other   = "other"


class NotificationChannel(str, enum.Enum):
    inapp  = "inapp"
    push   = "push"
    email  = "email"
    sms    = "sms"
    webhook = "webhook"


class NotificationStatus(str, enum.Enum):
    created   = "created"
    queued    = "queued"
    delivering = "delivering"
    delivered = "delivered"
    seen      = "seen"
    read      = "read"
    failed    = "failed"
    expired   = "expired"
    suppressed = "suppressed"  # e.g., user muted/unsubscribed


class NotificationPriority(str, enum.Enum):
    low    = "low"
    normal = "normal"
    high   = "high"
    urgent = "urgent"


_LANG_RE = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$")
_MAX_MSG_LEN = 8192


class Notification(Base):
    """
    Arifa kwa mtumiaji (audit/ops ready):
      - Uainishaji: type/channel/priority
      - CTA & deep link (cta_label/url, deep_link)
      - Lifecycle: created→queued→delivering→delivered→seen→read / failed / expired / suppressed
      - Dedupe: idempotency_key + collapse_key (client-side coalescing)
      - Context: actor_user, data/actions/meta (JSONB/JSON portable)
      - Delivery attempts: retry_count, last_attempt_at, provider_message_id
    """
    __tablename__ = "notifications"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Recipient & actor
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )

    recipient: Mapped["User"] = relationship(
        "User",
        foreign_keys=lambda: [Notification.user_id],
        back_populates="notifications_received",
        lazy="selectin",
        passive_deletes=True,
    )
    actor: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=lambda: [Notification.actor_user_id],
        back_populates="notifications_sent",
        lazy="selectin",
        passive_deletes=True,
    )

    # Classification
    type: Mapped[NotificationType] = mapped_column(
        SQLEnum(NotificationType, name="notification_type"),
        default=NotificationType.other, nullable=False, index=True,
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        SQLEnum(NotificationChannel, name="notification_channel"),
        default=NotificationChannel.inapp, nullable=False, index=True,
    )
    status: Mapped[NotificationStatus] = mapped_column(
        SQLEnum(NotificationStatus, name="notification_status"),
        default=NotificationStatus.created, nullable=False, index=True,
    )
    priority: Mapped[NotificationPriority] = mapped_column(
        SQLEnum(NotificationPriority, name="notification_priority"),
        default=NotificationPriority.normal, nullable=False, index=True,
    )

    # Content
    title:   Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    short:   Mapped[Optional[str]] = mapped_column(String(160))
    lang:    Mapped[Optional[str]] = mapped_column(String(8), index=True)

    # CTA / deep-link
    cta_label: Mapped[Optional[str]] = mapped_column(String(64))
    cta_url:   Mapped[Optional[str]] = mapped_column(String(512))
    deep_link: Mapped[Optional[str]] = mapped_column(String(512))

    # Structured payloads
    data:    Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    actions: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    meta:    Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Dedupe / correlation
    request_id:      Mapped[Optional[str]] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    collapse_key:    Mapped[Optional[str]] = mapped_column(String(120), index=True, doc="Coalesce duplicate toasts")

    # Flags
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # Delivery & lifecycle
    scheduled_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    delivered_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    seen_at:       Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    read_at:       Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    failed_at:     Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    expires_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Attempts / provider info
    retry_count:         Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_attempt_at:     Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    provider:            Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # Errors
    error_code:   Mapped[Optional[str]] = mapped_column(String(80), index=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text)

    # Audit
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"),
        nullable=False, index=True,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_notification_idem"),
        Index("ix_notif_user_created", "user_id", "created_at"),
        Index("ix_notif_status_time", "status", "created_at"),
        Index("ix_notif_type_priority", "type", "priority"),
        Index("ix_notif_channel_status", "channel", "status"),
        Index("ix_notif_scheduled", "scheduled_at"),
        Index("ix_notif_read_flag", "read"),
        Index("ix_notif_user_active", "user_id", "status", "expires_at"),  # “inbox” queries
        CheckConstraint("length(title) >= 2", name="ck_notif_title_len"),
        CheckConstraint("length(message) >= 1", name="ck_notif_message_len"),
        CheckConstraint(f"length(message) <= {_MAX_MSG_LEN}", name="ck_notif_message_max"),
        CheckConstraint(
            "expires_at IS NULL OR scheduled_at IS NULL OR expires_at >= scheduled_at",
            name="ck_notif_expiry_after_schedule",
        ),
        CheckConstraint(
            "(status <> 'queued') OR (scheduled_at IS NOT NULL)",
            name="ck_notif_queued_has_schedule",
        ),
        # status ↔ timestamps guards (light, cross-DB)
        CheckConstraint("(status <> 'delivered') OR (delivered_at IS NOT NULL)", name="ck_notif_delivered_ts"),
        CheckConstraint("(status <> 'seen') OR (seen_at IS NOT NULL)", name="ck_notif_seen_ts"),
        CheckConstraint("(status <> 'read') OR (read_at IS NOT NULL)", name="ck_notif_read_ts"),
        CheckConstraint("(status <> 'failed') OR (failed_at IS NOT NULL)", name="ck_notif_failed_ts"),
        CheckConstraint("retry_count >= 0", name="ck_notif_retry_nonneg"),
        {"extend_existing": True},
    )

    # -------- Hybrids --------
    @hybrid_property
    def is_unread(self) -> bool:
        return not self.read and self.status not in (NotificationStatus.expired, NotificationStatus.suppressed)

    @hybrid_property
    def is_active(self) -> bool:
        now = dt.datetime.now(dt.timezone.utc)
        return not (self.expires_at and now >= self.expires_at)

    @hybrid_property
    def is_sendable(self) -> bool:
        """Inastahili kutumwa sasa (inaweza kutumika na scheduler)."""
        if self.status not in (NotificationStatus.created, NotificationStatus.queued, NotificationStatus.failed):
            return False
        if not self.is_active:
            return False
        if self.scheduled_at and dt.datetime.now(dt.timezone.utc) < self.scheduled_at:
            return False
        return True

    # -------- Helpers (lifecycle) --------
    def mark_queued(self, *, when: Optional[dt.datetime] = None) -> None:
        self.status = NotificationStatus.queued
        self.scheduled_at = when or dt.datetime.now(dt.timezone.utc)

    def mark_delivering(self) -> None:
        self.status = NotificationStatus.delivering
        self.last_attempt_at = dt.datetime.now(dt.timezone.utc)

    def mark_delivered(self, *, provider_message_id: Optional[str] = None) -> None:
        self.status = NotificationStatus.delivered
        self.delivered_at = dt.datetime.now(dt.timezone.utc)
        if provider_message_id:
            self.provider_message_id = provider_message_id

    def mark_seen(self) -> None:
        self.status = NotificationStatus.seen
        self.seen_at = dt.datetime.now(dt.timezone.utc)

    def mark_read(self) -> None:
        self.read = True
        self.status = NotificationStatus.read
        self.read_at = dt.datetime.now(dt.timezone.utc)

    def mark_failed(self, *, code: Optional[str] = None, detail: Optional[str] = None) -> None:
        self.status = NotificationStatus.failed
        self.failed_at = dt.datetime.now(dt.timezone.utc)
        self.retry_count = (self.retry_count or 0) + 1
        if code:
            self.error_code = code.strip() or None
        if detail:
            self.error_detail = detail.strip() or None

    def expire(self) -> None:
        self.status = NotificationStatus.expired

    def suppress(self, *, reason: Optional[str] = None) -> None:
        self.status = NotificationStatus.suppressed
        if reason:
            self.error_detail = ((self.error_detail + " | ") if self.error_detail else "") + f"suppressed: {reason}"

    def snooze(self, *, minutes: int = 10) -> None:
        self.scheduled_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=max(1, int(minutes)))
        self.status = NotificationStatus.queued

    def set_collapse_key(self, key: Optional[str]) -> None:
        self.collapse_key = (key or "").strip() or None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Notification id={self.id} user={self.user_id} type={self.type} status={self.status} read={self.read}>"


# -------- Validators / Normalizers --------
@validates("title", "short", "cta_label", "cta_url", "deep_link",
           "request_id", "idempotency_key", "collapse_key",
           "provider_message_id", "provider", "error_code")
def _trim_texts(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    return v or None

@validates("message")
def _validate_message(_inst, _key, value: str) -> str:
    v = (value or "").strip()
    if not v:
        raise ValueError("message cannot be empty.")
    if len(v) > _MAX_MSG_LEN:
        v = v[:_MAX_MSG_LEN]
    return v

@validates("lang")
def _validate_lang(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    if not v or not _LANG_RE.match(v):
        return None
    return v


# -------- Event hooks --------
from sqlalchemy.event import listens_for

@listens_for(Notification, "before_insert")
def _notif_before_insert(_m, _c, t: Notification) -> None:
    # Normalize text
    if t.title:
        t.title = t.title.strip()
    if t.short:
        t.short = t.short.strip() or None
    if t.error_detail:
        t.error_detail = t.error_detail.strip() or None
    # Align timestamps with status (light guards)
    now = dt.datetime.now(dt.timezone.utc)
    if t.status == NotificationStatus.queued and not t.scheduled_at:
        t.scheduled_at = now
    if t.status == NotificationStatus.delivered and not t.delivered_at:
        t.delivered_at = now
    if t.status == NotificationStatus.seen and not t.seen_at:
        t.seen_at = now
    if t.status == NotificationStatus.read and not t.read_at:
        t.read_at = now
    if t.status == NotificationStatus.failed and not t.failed_at:
        t.failed_at = now

@listens_for(Notification, "before_update")
def _notif_before_update(_m, _c, t: Notification) -> None:
    # Normalize
    if t.title:
        t.title = t.title.strip()
    if t.short:
        t.short = t.short.strip() or None
    if t.error_detail:
        t.error_detail = t.error_detail.strip() or None
    # Auto-expire if needed
    if t.expires_at and dt.datetime.now(dt.timezone.utc) >= t.expires_at and t.status not in (
        NotificationStatus.read, NotificationStatus.seen, NotificationStatus.delivered, NotificationStatus.expired
    ):
        t.status = NotificationStatus.expired
    # Keep timestamp invariants consistent
    now = dt.datetime.now(dt.timezone.utc)
    if t.status == NotificationStatus.queued and not t.scheduled_at:
        t.scheduled_at = now
    if t.status == NotificationStatus.delivered and not t.delivered_at:
        t.delivered_at = now
    if t.status == NotificationStatus.seen and not t.seen_at:
        t.seen_at = now
    if t.status == NotificationStatus.read:
        t.read = True
        if not t.read_at:
            t.read_at = now
    if t.status == NotificationStatus.failed and not t.failed_at:
        t.failed_at = now
