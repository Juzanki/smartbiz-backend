# === backend/models/scheduled_message.py ===
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

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
    text as sa_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json


def _utcnow() -> datetime:
    """Aware UTC timestamp (inatumika kwa helpers)."""
    return datetime.now(timezone.utc)


class SchedPlatform(str, enum.Enum):
    telegram = "telegram"
    whatsapp = "whatsapp"
    sms      = "sms"
    email    = "email"
    custom   = "custom"
    other    = "other"


class SchedStatus(str, enum.Enum):
    pending  = "pending"   # imesajiliwa, inasubiri kuwekewa foleni
    queued   = "queued"    # iko tayari kusafirishwa/imepangwa
    sending  = "sending"   # jaribio la kutuma linaendelea
    sent     = "sent"      # limefanikiwa
    failed   = "failed"    # limeshindikana na retries zimeisha (au zimezimwa)
    canceled = "canceled"  # limebatilishwa na mtumiaji/mtandao
    paused   = "paused"    # limesitishwa kwa muda (lisipelekwe)


class ScheduledMessage(Base):
    """
    Ujumbe uliopangwa (scheduler-friendly model).
    - Idempotency + (platform, provider_message_id) dedupe
    - Retries na exponential backoff (≤ 1h)
    - `can_dispatch_now` kusaidia job ya dispatcher
    """
    __tablename__ = "scheduled_messages"
    __mapper_args__ = {"eager_defaults": True}

    # ---------- Identity / ownership ----------
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user: Mapped["User"] = relationship(
        "User", back_populates="scheduled_messages", lazy="selectin", passive_deletes=True
    )

    # ---------- Target & content ----------
    recipient: Mapped[str] = mapped_column(String(191), nullable=False, index=True)  # phone/chat_id/handle
    content: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[SchedPlatform] = mapped_column(
        SQLEnum(SchedPlatform, name="sched_platform", native_enum=False, validate_strings=True),
        default=SchedPlatform.telegram,
        nullable=False,
        index=True,
    )
    attachments: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(as_mutable_json(JSON_VARIANT))  # [{"type":"image","url":"..."}]

    # ---------- Scheduling & state ----------
    status: Mapped[SchedStatus] = mapped_column(
        SQLEnum(SchedStatus, name="sched_status", native_enum=False, validate_strings=True),
        default=SchedStatus.pending,
        nullable=False,
        index=True,
    )
    scheduled_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    # Legacy flag (kwa backward compatibility na code ya zamani)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # Dispatch lifecycle
    queued_at:        Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    sent_at:          Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    failed_at:        Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    canceled_at:      Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_attempt_at:  Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    next_attempt_at:  Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    retry_count:      Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    max_retries:      Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("3"))

    # ---------- Provider metadata / idempotency ----------
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    idempotency_key:     Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    request_id:          Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # Hifadhi response/headers/diagnostics ndogo
    meta_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # ---------- Audit ----------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ---------- Constraints / indexes ----------
    __table_args__ = (
        # dedupe kwa ujumbe uliokwisha pelekwa na provider
        UniqueConstraint("platform", "provider_message_id", name="uq_sched_platform_provider_msg"),
        # utafutaji wa kawaida wa scheduler
        Index("ix_sched_user_time", "user_id", "scheduled_time"),
        Index("ix_sched_status_time", "status", "scheduled_time"),
        Index("ix_sched_next_attempt", "next_attempt_at"),
        # ulinzi wa data
        CheckConstraint("length(recipient) >= 3", name="ck_sched_recipient_len"),
        CheckConstraint("length(content) >= 1", name="ck_sched_content_len"),
        CheckConstraint("retry_count >= 0 AND max_retries >= 0", name="ck_sched_retry_nonneg"),
        CheckConstraint(
            "(next_attempt_at IS NULL) OR (scheduled_time IS NULL) OR (next_attempt_at >= scheduled_time)",
            name="ck_sched_next_after_sched",
        ),
    )

    # ---------- Properties ----------
    @property
    def is_terminal(self) -> bool:
        return self.status in (SchedStatus.sent, SchedStatus.failed, SchedStatus.canceled)

    @property
    def is_sent(self) -> bool:
        """Alias ya kisasa kwa `sent` (legacy flag)."""
        return self.status == SchedStatus.sent

    @property
    def can_retry(self) -> bool:
        if self.is_terminal and self.status != SchedStatus.failed:
            return False
        return (self.retry_count or 0) < (self.max_retries or 0)

    @property
    def can_dispatch_now(self) -> bool:
        """Je, dispatcher anaweza kujaribu kutuma sasa?"""
        if self.status not in (SchedStatus.pending, SchedStatus.queued, SchedStatus.sending):
            return False
        now = _utcnow()
        if self.scheduled_time and now < self.scheduled_time:
            return False
        if self.next_attempt_at and now < self.next_attempt_at:
            return False
        return True

    # ---------- Helpers ----------
    def queue(self, when: Optional[datetime] = None) -> None:
        """Weka kwenye foleni au ipange kutumwa wakati maalum."""
        self.status = SchedStatus.queued
        if when:
            self.scheduled_time = when
            self.next_attempt_at = when
        else:
            self.next_attempt_at = _utcnow()
        self.queued_at = self.queued_at or _utcnow()
        self.failed_at = None
        self.sent = False

    def reschedule(self, when: datetime) -> None:
        """Badilisha muda wa kutuma na iwe queued."""
        self.scheduled_time = when
        self.next_attempt_at = when
        self.status = SchedStatus.queued
        self.queued_at = _utcnow()

    def mark_sending(self) -> None:
        self.status = SchedStatus.sending
        self.last_attempt_at = _utcnow()

    def mark_sent(self, provider_id: Optional[str] = None) -> None:
        """Weka alama kuwa imetumwa kwa mafanikio."""
        self.sent = True
        self.status = SchedStatus.sent
        self.sent_at = _utcnow()
        self.updated_at = _utcnow()
        if provider_id:
            self.provider_message_id = provider_id
        # on success: safisha error meta
        if self.meta_json:
            self.meta_json.pop("last_error", None)

    def backoff_retry(self, *, base_seconds: int = 30, cap_seconds: int = 3600) -> None:
        """Exponential backoff: min(cap, base * 2^(retry-1))."""
        self.retry_count = (self.retry_count or 0) + 1
        delay = min(cap_seconds, max(base_seconds, base_seconds * (2 ** max(0, self.retry_count - 1))))
        self.last_attempt_at = _utcnow()
        self.next_attempt_at = self.last_attempt_at + timedelta(seconds=delay)
        # Ikiwa bado tuna attempts, turudishe queued; la sivyo failed
        self.status = SchedStatus.queued if self.can_retry else SchedStatus.failed
        if self.status == SchedStatus.failed:
            self.failed_at = self.failed_at or _utcnow()

    def mark_failed(self, error: Optional[str] = None, *, base_seconds: int = 30) -> None:
        """
        Rekodi jaribio lililoshindikana.
        - Itaongeza retry_count na kupanga next_attempt_at (exponential backoff).
        - Ikitumia attempts zote, itaweka status=failed.
        """
        # hifadhi ujumbe wa kosa
        if error:
            self.meta_json = {**(self.meta_json or {}), "last_error": error}
        # tumia backoff
        self.backoff_retry(base_seconds=base_seconds)
        self.sent = False

    def cancel(self, reason: Optional[str] = None) -> None:
        self.status = SchedStatus.canceled
        self.canceled_at = _utcnow()
        self.next_attempt_at = None
        if reason:
            self.meta_json = {**(self.meta_json or {}), "canceled_reason": reason}

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ScheduledMessage id={self.id} user={self.user_id} platform={self.platform} "
            f"to={self.recipient} at={self.scheduled_time!s} status={self.status} sent={self.sent}>"
        )
