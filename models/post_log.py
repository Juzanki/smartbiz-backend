# backend/models/post_log.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import enum
import hashlib
from typing import Optional, List

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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# ---- Portable JSON already handled by JSON_VARIANT in _types ----

class PostPlatform(str, enum.Enum):
    telegram  = "telegram"
    whatsapp  = "whatsapp"
    instagram = "instagram"
    facebook  = "facebook"
    twitter   = "twitter"
    tiktok    = "tiktok"
    sms       = "sms"
    email     = "email"
    other     = "other"

class PostLogStatus(str, enum.Enum):
    pending   = "pending"      # imeundwa
    queued    = "queued"       # iko foleni kutumwa ASAP
    scheduled = "scheduled"    # itatumwa wakati maalum
    sent      = "sent"
    failed    = "failed"
    canceled  = "canceled"

class PostPriority(str, enum.Enum):
    low    = "low"
    normal = "normal"
    high   = "high"
    urgent = "urgent"

class ContentFormat(str, enum.Enum):
    plain    = "plain"
    markdown = "markdown"
    html     = "html"

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


class PostLog(Base):
    """
    Historia ya machapisho yaliyotumwa na Social Media Bot.

    - Dedupe: idempotency_key + (platform, external_message_id) + content_hash
    - Recipient/thread refs
    - Retry metadata: attempt_count, next_attempt_at, backoff exponential
    - Dispatch helpers: ready_for_dispatch(), queue(), schedule_for()
    - Validation/normalization ya text & language; content_format & priority
    """
    __tablename__ = "post_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Owner (business user / bot owner)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user: Mapped["User"] = relationship(
        "User", back_populates="post_logs", passive_deletes=True, lazy="selectin"
    )

    # Target / destination
    platform: Mapped[PostPlatform] = mapped_column(
        SQLEnum(PostPlatform, name="post_platform", native_enum=False, validate_strings=True),
        nullable=False,
        index=True,
    )
    recipient_ref: Mapped[Optional[str]] = mapped_column(String(160))  # phone/@username/chat_id/list-id
    thread_ref:    Mapped[Optional[str]] = mapped_column(String(160))  # thread/topic id

    # Content
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_format: Mapped[ContentFormat] = mapped_column(
        SQLEnum(ContentFormat, name="post_content_format", native_enum=False, validate_strings=True),
        default=ContentFormat.plain, nullable=False, index=True,
    )
    attachments: Mapped[Optional[List[dict]]] = mapped_column(as_mutable_json(JSON_VARIANT))  # [{"type":"image","url":"..."}]
    language:    Mapped[Optional[str]] = mapped_column(String(8), index=True)  # "en","sw",...

    # Classification & delivery hints
    priority: Mapped[PostPriority] = mapped_column(
        SQLEnum(PostPriority, name="post_priority", native_enum=False, validate_strings=True),
        default=PostPriority.normal, nullable=False, index=True,
    )
    silent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)  # e.g. silent push

    # Status & error
    status: Mapped[PostLogStatus] = mapped_column(
        SQLEnum(PostLogStatus, name="post_log_status", native_enum=False, validate_strings=True),
        default=PostLogStatus.pending, nullable=False, index=True,
    )
    error_code:    Mapped[Optional[str]] = mapped_column(String(80), index=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    # External refs / dedupe
    external_message_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)  # returned by platform
    external_url:        Mapped[Optional[str]] = mapped_column(String(512))
    idempotency_key:     Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    request_id:          Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # Content dedupe (useful to collapse repeats to same recipient/platform)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    collapse_key: Mapped[Optional[str]] = mapped_column(String(120), index=True)  # "tg:chat123|promo-aug"

    # Scheduling & retry
    scheduled_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    sent_at:         Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    failed_at:       Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    attempt_count:   Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts:    Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    # optional send window
    deliver_after:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    expires_at:      Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Extra payloads / audit
    meta: Mapped[Optional[dict]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ---------- Indexes / Guards ----------
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_postlog_idem"),
        UniqueConstraint("platform", "external_message_id", name="uq_postlog_platform_external"),
        Index("ix_postlog_user_created", "user_id", "created_at"),
        Index("ix_postlog_platform_status", "platform", "status"),
        Index("ix_postlog_sched_next", "scheduled_at", "next_attempt_at"),
        Index("ix_postlog_status_time", "status", "created_at"),
        Index("ix_postlog_recipient", "recipient_ref"),
        Index("ix_postlog_collapse", "platform", "recipient_ref", "collapse_key"),
        Index("ix_postlog_hash_platform_recipient", "platform", "recipient_ref", "content_hash"),
        CheckConstraint("length(content) >= 1", name="ck_postlog_content_len"),
        CheckConstraint("attempt_count >= 0", name="ck_postlog_attempts_nonneg"),
        CheckConstraint("max_attempts >= 0", name="ck_postlog_max_attempts_nonneg"),
        CheckConstraint(
            "(expires_at IS NULL) OR (created_at IS NULL) OR (expires_at >= created_at)",
            name="ck_postlog_expiry_after_create",
        ),
        CheckConstraint(
            "(scheduled_at IS NULL) OR (created_at IS NULL) OR (scheduled_at >= created_at)",
            name="ck_postlog_sched_after_create",
        ),
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def is_terminal(self) -> bool:
        return self.status in (PostLogStatus.sent, PostLogStatus.failed, PostLogStatus.canceled)

    @hybrid_property
    def is_scheduled(self) -> bool:
        return self.status == PostLogStatus.scheduled and self.scheduled_at is not None

    @hybrid_property
    def ready_for_dispatch(self) -> bool:
        """Je, inapaswa kujaribiwa kutumwa sasa? (scheduler/worker can use this)"""
        if self.is_terminal:
            return False
        now = _utcnow()
        if self.expires_at and now >= self.expires_at:
            return False
        if self.deliver_after and now < self.deliver_after:
            return False
        if self.next_attempt_at and now < self.next_attempt_at:
            return False
        if self.is_scheduled and self.scheduled_at and now < self.scheduled_at:
            return False
        return self.status in (PostLogStatus.pending, PostLogStatus.queued, PostLogStatus.scheduled, PostLogStatus.failed)

    # ---------- Validators / Normalizers ----------
    @validates("recipient_ref", "thread_ref", "external_message_id", "external_url",
               "idempotency_key", "request_id", "error_code", "error_message",
               "collapse_key", "language")
    def _trim(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if _k == "language":
            s = s[:8].lower()
        return s or None

    @validates("content")
    def _hash_and_trim_content(self, _k: str, v: str) -> str:
        s = (v or "").strip()
        self.content_hash = _sha256(s) if s else None
        return s

    # ---------- Helpers ----------
    def queue(self) -> None:
        """Weka kwenye foleni ya sasa (ASAP)."""
        self.status = PostLogStatus.queued
        self.scheduled_at = None
        self.error_code = None
        self.error_message = None
        self.next_attempt_at = self.next_attempt_at or _utcnow()

    def schedule_for(self, when: dt.datetime) -> None:
        """Panga kutumwa tarehe/saa maalum."""
        self.status = PostLogStatus.scheduled
        self.scheduled_at = when
        self.next_attempt_at = when
        self.error_code = None
        self.error_message = None

    def mark_sent(self, *, external_id: Optional[str] = None, url: Optional[str] = None) -> None:
        self.status = PostLogStatus.sent
        self.sent_at = _utcnow()
        if external_id:
            self.external_message_id = external_id
        if url:
            self.external_url = url
        self.error_code = None
        self.error_message = None

    def mark_failed(self, *, code: Optional[str] = None, message: Optional[str] = None) -> None:
        """
        Rekodi jaribio lililoshindikana; weka **exponential backoff** kwa retry (ikiwa attempts bado zipo).
        backoff = min(1h, 30s * 2^(attempt_count-1))
        """
        now = _utcnow()
        self.last_attempt_at = now
        self.attempt_count = (self.attempt_count or 0) + 1
        self.error_code = code
        self.error_message = message

        if (self.attempt_count or 0) >= (self.max_attempts or 0):
            self.status = PostLogStatus.failed
            self.failed_at = self.failed_at or now
            self.next_attempt_at = None
            return

        base = 30  # seconds
        delay = min(3600, base * (2 ** max(0, (self.attempt_count - 1))))
        self.next_attempt_at = now + dt.timedelta(seconds=delay)
        self.status = PostLogStatus.queued

    def cancel(self) -> None:
        self.status = PostLogStatus.canceled
        self.scheduled_at = None
        self.next_attempt_at = None
        self.error_code = None
        self.error_message = None

    def set_window(self, *, deliver_after: Optional[dt.datetime] = None, expires_at: Optional[dt.datetime] = None) -> None:
        """Weka dirisha la usafirishaji (earliest/latest)."""
        self.deliver_after = deliver_after
        self.expires_at = expires_at

    def collapse_with(self, other: "PostLog") -> bool:
        """
        Je, hii inaweza kuunganishwa na nyingine ili kuzuia “spam”?
        Kanuni: same platform + recipient + (collapse_key or content_hash).
        """
        if self.platform != other.platform or self.recipient_ref != other.recipient_ref:
            return False
        if self.collapse_key and other.collapse_key:
            return self.collapse_key == other.collapse_key
        return bool(self.content_hash and self.content_hash == other.content_hash)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PostLog id={self.id} user={self.user_id} {self.platform} {self.status} prio={self.priority}>"
