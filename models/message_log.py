# backend/models/message_log.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import hashlib
import re
import datetime as dt
from typing import Optional, TYPE_CHECKING, List, Dict, Any

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


# ---------- Enums ----------
class MsgOrigin(str, enum.Enum):
    telegram = "telegram"
    whatsapp = "whatsapp"
    sms      = "sms"
    web      = "web"
    email    = "email"
    webhook  = "webhook"
    internal = "internal"
    other    = "other"


class MsgDirection(str, enum.Enum):
    inbound  = "inbound"
    outbound = "outbound"


class MsgStatus(str, enum.Enum):
    received  = "received"
    processed = "processed"
    failed    = "failed"
    ignored   = "ignored"
    retried   = "retried"


# ---------- Helpers ----------
_LANG_RE = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$")  # e.g., 'en' or 'en-US'
_MAX_CONTENT_LEN = 8192


def _sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


# ---------- Model ----------
class MessageLog(Base):
    """Rekodi ya ujumbe kutoka/kuelekea majukwaa (Telegram/WhatsApp/SMS/Web n.k.)."""
    __tablename__ = "message_logs"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Link to app user (hiari)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    user: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="message_logs",
        foreign_keys=[user_id],
        lazy="selectin",
        passive_deletes=True,
    )

    # Conversation identifiers
    chat_id: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    platform_message_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    thread_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    reply_to_message_id: Mapped[Optional[str]] = mapped_column(String(160))

    # Sender identity on platform
    sender_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    username: Mapped[Optional[str]] = mapped_column(String(160), index=True)

    # Classification
    origin: Mapped[MsgOrigin] = mapped_column(
        SQLEnum(MsgOrigin, name="msglog_origin"), default=MsgOrigin.other, nullable=False, index=True
    )
    direction: Mapped[MsgDirection] = mapped_column(
        SQLEnum(MsgDirection, name="msglog_direction"), default=MsgDirection.inbound, nullable=False, index=True
    )
    status: Mapped[MsgStatus] = mapped_column(
        SQLEnum(MsgStatus, name="msglog_status"), default=MsgStatus.received, nullable=False, index=True
    )

    # Content
    content: Mapped[str] = mapped_column(Text, nullable=False)
    lang: Mapped[Optional[str]] = mapped_column(String(8), index=True)
    attachments: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    hashtags: Mapped[Optional[List[str]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    raw_payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Delivery & audit
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    processed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    retried_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_code: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text)

    # Correlation / dedupe
    request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)

    # Flags
    is_command: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_msglog_idem"),
        UniqueConstraint("chat_id", "platform_message_id", name="uq_msglog_chat_platform_msg"),
        Index("ix_msglog_user_time", "user_id", "received_at"),
        Index("ix_msglog_chat_time", "chat_id", "received_at"),
        Index("ix_msglog_status_time", "status", "received_at"),
        Index("ix_msglog_origin_dir", "origin", "direction"),
        Index("ix_msglog_request", "request_id"),
        CheckConstraint("length(content) >= 1", name="ck_msglog_content_len"),
        CheckConstraint(f"length(content) <= {_MAX_CONTENT_LEN}", name="ck_msglog_content_max"),
        CheckConstraint("retried_count >= 0", name="ck_msglog_retry_nonneg"),
        # Guard ndogo: processed ⇒ lazima processed_at; failed ⇒ failed_at
        CheckConstraint(
            "(status <> 'processed') OR (processed_at IS NOT NULL)",
            name="ck_msglog_processed_ts",
        ),
        CheckConstraint(
            "(status <> 'failed') OR (failed_at IS NOT NULL)",
            name="ck_msglog_failed_ts",
        ),
        {"extend_existing": True},
    )

    # ---------- Hybrids / compatibility ----------
    @hybrid_property
    def message(self) -> str:
        """Alias ya nyuma kwa `content` (compat na code ya zamani)."""
        return self.content

    @message.setter
    def message(self, value: str) -> None:
        self.content = value

    # ---------- Helpers (domain) ----------
    def mark_processed(self) -> None:
        self.status = MsgStatus.processed
        self.processed_at = dt.datetime.now(dt.timezone.utc)

    def mark_failed(self, *, code: str | None = None, detail: str | None = None) -> None:
        self.status = MsgStatus.failed
        self.failed_at = dt.datetime.now(dt.timezone.utc)
        if code:
            self.error_code = code.strip() or None
        if detail:
            self.error_detail = detail.strip() or None

    def mark_ignored(self) -> None:
        self.status = MsgStatus.ignored

    def mark_retried(self) -> None:
        self.status = MsgStatus.retried
        self.retried_count = (self.retried_count or 0) + 1

    def set_command(self, on: bool = True) -> None:
        self.is_command = bool(on)

    def set_private(self, on: bool = True) -> None:
        self.is_private = bool(on)

    def ensure_idempotency_from_payload(self) -> None:
        """
        Ikiwa `idempotency_key` haipo, tengeneza stable key kutoka chat_id + platform_message_id
        au hash ya raw_payload (fallback).
        """
        if self.idempotency_key:
            return
        if self.chat_id and self.platform_message_id:
            self.idempotency_key = f"{self.chat_id}:{self.platform_message_id}"
        elif self.raw_payload:
            self.idempotency_key = _sha256(repr(self.raw_payload))[:120]

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<MessageLog id={self.id} chat={self.chat_id} user={self.user_id} "
            f"origin={self.origin} dir={self.direction} status={self.status}>"
        )


# ---------- Validators ----------
@validates("content")
def _validate_content(_inst, _key, value: str) -> str:
    v = (value or "").strip()
    if not v:
        raise ValueError("content cannot be empty.")
    if len(v) > _MAX_CONTENT_LEN:
        v = v[:_MAX_CONTENT_LEN]
    return v

@validates("lang")
def _validate_lang(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if not _LANG_RE.match(v):
        return None
    return v

@validates("chat_id", "platform_message_id", "thread_id", "reply_to_message_id",
           "sender_id", "username", "request_id", "idempotency_key", "error_code")
def _trim_short_texts(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    return v or None


# ---------- Event hooks ----------
from sqlalchemy.event import listens_for

@listens_for(MessageLog, "before_insert")
def _msglog_before_insert(_m, _c, t: MessageLog) -> None:
    # Normalize strings
    if t.content:
        t.content = t.content.strip()
    if t.error_detail:
        t.error_detail = t.error_detail.strip() or None
    # Auto idempotency kama haikuletwa
    t.ensure_idempotency_from_payload()
    # Timestamps guards (in case status already set)
    now = dt.datetime.now(dt.timezone.utc)
    if t.status == MsgStatus.processed and not t.processed_at:
        t.processed_at = now
    if t.status == MsgStatus.failed and not t.failed_at:
        t.failed_at = now

@listens_for(MessageLog, "before_update")
def _msglog_before_update(_m, _c, t: MessageLog) -> None:
    # keep text fields tidy
    if t.content:
        t.content = t.content.strip()
    if t.error_detail:
        t.error_detail = t.error_detail.strip() or None
    # Rebuild idempotency if still empty and payload exists
    if not t.idempotency_key:
        t.ensure_idempotency_from_payload()
    # Align timestamps with status
    now = dt.datetime.now(dt.timezone.utc)
    if t.status == MsgStatus.processed and not t.processed_at:
        t.processed_at = now
    if t.status == MsgStatus.failed and not t.failed_at:
        t.failed_at = now
