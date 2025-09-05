# backend/models/webhook_delivery_log.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from typing import Optional, Dict, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

class WebhookDeliveryLog(Base):
    """
    Audit log ya utoaji wa webhooks:
    - Typed mappings (SQLAlchemy 2.0)
    - TZ-aware timestamps
    - Uhusiano thabiti na WebhookEndpoint + User
    - Maelezo ya majaribio/retries na ufuatiliaji
    """
    __tablename__ = "webhook_delivery_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Umiliki
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    endpoint_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("webhook_endpoints.id", ondelete="SET NULL"),
        nullable=True, index=True
    )

    # Lengo + tukio
    target_url: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    payload: Mapped[Optional[str]] = mapped_column(Text, default=None)

    # Metadata ya ombi
    request_id: Mapped[Optional[str]] = mapped_column(String(64), default=None, index=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(64), default=None, index=True)
    headers: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)
    signature: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    verified_signature: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Majibu ya server
    response_code: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    response_body: Mapped[Optional[str]] = mapped_column(Text, default=None)
    error_message: Mapped[Optional[str]] = mapped_column(String(255), default=None)

    # Hali ya usafirishaji
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    backoff_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    next_retry_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, default=None)

    # Wakati
    sent_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships — MAJINA YAKE YALAZIMA YAOANE NA KILE KILICHO KWENYE User/WebhookEndpoint
    user: Mapped["User"] = relationship("User", back_populates="webhook_delivery_logs", lazy="selectin")
    endpoint: Mapped[Optional["WebhookEndpoint"]] = relationship(
        "WebhookEndpoint", back_populates="deliveries", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("attempt >= 1", name="ck_webhook_attempt_min1"),
        CheckConstraint("max_retries >= 0", name="ck_webhook_max_retries_nonneg"),
        CheckConstraint("backoff_seconds >= 0", name="ck_webhook_backoff_nonneg"),
        Index("ix_webhook_logs_user_event_time", "user_id", "event_type", "sent_at"),
        Index("ix_webhook_logs_endpoint_time", "endpoint_id", "sent_at"),
        Index("ix_webhook_logs_success_time", "success", "sent_at"),
        Index("ix_webhook_logs_req_corr", "request_id", "correlation_id"),
    )

    # ------------------------ Helpers (hakuna DB I/O hapa) ------------------------ #
    def mark_attempt(
        self,
        *,
        attempt: Optional[int] = None,
        sent_at: Optional[dt.datetime] = None,
        headers: Optional[Dict[str, Any]] = None,
        signature: Optional[str] = None,
        verified: Optional[bool] = None,
    ) -> None:
        if attempt is not None and attempt >= 1:
            self.attempt = attempt
        if sent_at:
            self.sent_at = sent_at
        if headers is not None:
            self.headers = headers
        if signature is not None:
            self.signature = signature[:128]
        if verified is not None:
            self.verified_signature = bool(verified)

    def mark_success(self, *, status_code: int, body: Optional[str], duration_ms: Optional[int] = None) -> None:
        self.success = True
        self.response_code = int(status_code)
        self.response_body = body
        self.duration_ms = duration_ms
        self.next_retry_at = None
        self.error_message = None

    def mark_failure(
        self,
        *,
        status_code: Optional[int] = None,
        body: Optional[str] = None,
        error: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        self.success = False
        self.response_code = int(status_code) if status_code is not None else None
        self.response_body = body
        self.error_message = (error or "")[:255] if error else self.error_message
        self.duration_ms = duration_ms

    def schedule_retry(self, now: Optional[dt.datetime] = None) -> None:
        if self.attempt >= self.max_retries:
            self.next_retry_at = None
            return
        base = self.backoff_seconds or 0
        delay = base * (2 ** max(self.attempt - 1, 0))
        now_ts = now or dt.datetime.now(dt.timezone.utc)
        self.next_retry_at = now_ts + dt.timedelta(seconds=delay)

    def __repr__(self) -> str:  # pragma: no cover
        ok = "ok" if self.success else "fail"
        return f"<WebhookDeliveryLog id={self.id} user={self.user_id} event={self.event_type} attempt={self.attempt} status={ok} code={self.response_code}>"



