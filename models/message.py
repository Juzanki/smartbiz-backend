# backend/models/message.py
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
    from .live_stream import LiveStream


# ------------- Enums -------------
class MessageStatus(str, enum.Enum):
    active  = "active"
    hidden  = "hidden"
    flagged = "flagged"
    deleted = "deleted"


class MessageType(str, enum.Enum):
    user   = "user"
    system = "system"
    action = "action"
    bot    = "bot"
    event  = "event"


class MessageSource(str, enum.Enum):
    user      = "user"
    moderator = "moderator"
    system    = "system"
    bot       = "bot"
    webhook   = "webhook"


class ContentFormat(str, enum.Enum):
    plain    = "plain"
    markdown = "markdown"
    html     = "html"
    json     = "json"


# ------------- Helpers -------------
_LANG_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")   # e.g. 'en' or 'en-US'
_MAX_CONTENT_LEN = 8_192                            # kikomo salama kwa ujumbe mmoja


def _hash_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


# ------------- Model -------------
class Message(Base):
    """
    Chat message (audit/ops ready):
      - Threading: parent/replies + thread_id (root id)
      - Scope: live_stream_id au room_id (angalau moja lazima)
      - Moderation: status/pin/flags + timestamps
      - Edits: edited_at/edit_count + content_hash
      - JSON: mentions/attachments/meta (mutable & portable)
      - Dedupe: idempotency_key
    """
    __tablename__ = "messages"
    __mapper_args__ = {"eager_defaults": True}

    # ---- Columns ----
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Author
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    user: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[user_id], lazy="selectin", passive_deletes=True
    )

    # Scope (angalau moja kati ya hizi mbili inahitajika)
    live_stream_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("live_streams.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    room_id: Mapped[Optional[str]] = mapped_column(String(120), index=True, nullable=True)
    stream: Mapped[Optional["LiveStream"]] = relationship(
        "LiveStream", foreign_keys=[live_stream_id], lazy="selectin", passive_deletes=True
    )

    # Threading (self-referential)
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    thread_id: Mapped[Optional[int]] = mapped_column(
        Integer, index=True, doc="Root message id of the thread"
    )

    parent: Mapped["Message | None"] = relationship(
        "Message",
        back_populates="replies",
        remote_side=lambda: [Message.id],
        foreign_keys=lambda: [Message.parent_id],
        passive_deletes=True,
        uselist=False,
        lazy="selectin",
    )
    replies: Mapped[List["Message"]] = relationship(
        "Message",
        back_populates="parent",
        foreign_keys=lambda: [Message.parent_id],
        cascade="all, delete-orphan",
        single_parent=True,
        passive_deletes=True,
        lazy="selectin",
    )

    reply_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Content
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_format: Mapped[ContentFormat] = mapped_column(
        SQLEnum(ContentFormat, name="message_content_format"),
        default=ContentFormat.plain,
        nullable=False,
        index=True,
    )
    lang: Mapped[Optional[str]] = mapped_column(String(8), index=True)
    mentions: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    attachments: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Classification
    type: Mapped[MessageType] = mapped_column(
        SQLEnum(MessageType, name="message_type"),
        default=MessageType.user,
        nullable=False,
        index=True,
    )
    source: Mapped[MessageSource] = mapped_column(
        SQLEnum(MessageSource, name="message_source"),
        default=MessageSource.user,
        nullable=False,
        index=True,
    )
    status: Mapped[MessageStatus] = mapped_column(
        SQLEnum(MessageStatus, name="message_status"),
        default=MessageStatus.active,
        nullable=False,
        index=True,
    )
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # Moderation & edits
    moderation_reason: Mapped[Optional[str]] = mapped_column(String(160))
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    edit_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    edited_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    hidden_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    flagged_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Dedupe/correlation
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_message_idem"),
        Index("ix_messages_user_created", "user_id", "created_at"),
        Index("ix_messages_stream_time", "live_stream_id", "created_at"),
        Index("ix_messages_room_time", "room_id", "created_at"),
        Index("ix_messages_status_time", "status", "created_at"),
        Index("ix_messages_thread_time", "thread_id", "created_at"),
        Index("ix_messages_parent", "parent_id"),
        Index("ix_messages_hash", "content_hash"),
        # Guards
        CheckConstraint("length(content) >= 1", name="ck_message_content_len"),
        CheckConstraint(f"length(content) <= {_MAX_CONTENT_LEN}", name="ck_message_content_max"),
        CheckConstraint("reply_count >= 0", name="ck_message_reply_count_nonneg"),
        # Angalau scope moja lazima (live_stream_id au room_id)
        CheckConstraint(
            "(live_stream_id IS NOT NULL) OR (room_id IS NOT NULL)",
            name="ck_message_scope_present",
        ),
        {"extend_existing": True},
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def is_edited(self) -> bool:
        return (self.edit_count or 0) > 0

    @hybrid_property
    def is_visible(self) -> bool:
        return self.status == MessageStatus.active and self.deleted_at is None

    @hybrid_property
    def preview(self) -> str:
        return (self.content or "")[:140]

    @hybrid_property
    def is_thread_root(self) -> bool:
        return self.id is not None and self.thread_id == self.id

    # ---------- Helpers ----------
    @staticmethod
    def _hash(text_value: str) -> str:
        return _hash_text(text_value)

    def ensure_thread_id(self) -> None:
        """Set thread_id to parent or self (itawekwa pia kwenye hooks)."""
        if not self.thread_id:
            self.thread_id = self.parent_id or self.id

    def edit(self, new_content: str) -> None:
        self.content = (new_content or "").strip()
        self.content_hash = self._hash(self.content)
        self.edit_count = (self.edit_count or 0) + 1
        self.edited_at = dt.datetime.now(dt.timezone.utc)

    def soft_delete(self, *, reason: str | None = None) -> None:
        self.status = MessageStatus.deleted
        self.deleted_at = dt.datetime.now(dt.timezone.utc)
        if reason:
            self.moderation_reason = reason.strip() or None

    def hide(self, *, reason: str | None = None) -> None:
        self.status = MessageStatus.hidden
        self.hidden_at = dt.datetime.now(dt.timezone.utc)
        if reason:
            self.moderation_reason = reason.strip() or None

    def unhide(self) -> None:
        self.status = MessageStatus.active
        self.hidden_at = None

    def flag(self, *, reason: str | None = None) -> None:
        self.status = MessageStatus.flagged
        self.flagged_at = dt.datetime.now(dt.timezone.utc)
        if reason:
            self.moderation_reason = reason.strip() or None

    def pin(self) -> None:
        self.is_pinned = True

    def unpin(self) -> None:
        self.is_pinned = False

    def __repr__(self) -> str:  # pragma: no cover
        scope = self.live_stream_id or self.room_id
        return f"<Message id={self.id} user={self.user_id} scope={scope} status={self.status}>"


# ---------------- Validators / Normalizers ----------------
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
        # bad lang -> weka None kuliko kuhifadhi takataka
        return None
    return v

@validates("moderation_reason", "room_id", "idempotency_key", "request_id")
def _trim_texts(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    return v or None


# ---------------- Event hooks ----------------
from sqlalchemy.event import listens_for

@listens_for(Message.replies, "append")
def _on_reply_append(parent: Message, child: Message, _initiator):
    # auto-thread: reply inherits thread_id ya mzazi
    if child.parent_id != parent.id:
        child.parent_id = parent.id
    child.thread_id = parent.thread_id or parent.id
    parent.reply_count = (parent.reply_count or 0) + 1

@listens_for(Message.replies, "remove")
def _on_reply_remove(parent: Message, child: Message, _initiator):
    parent.reply_count = max(0, (parent.reply_count or 0) - 1)

@listens_for(Message, "before_insert")
def _msg_before_insert(_mapper, _conn, t: Message) -> None:
    # content hash ya mwanzo
    if t.content:
        t.content = t.content.strip()
        t.content_hash = t.content_hash or _hash_text(t.content)
    # thread_id auto
    if not t.thread_id:
        t.thread_id = t.parent_id or t.id
    # normalize strings
    if t.moderation_reason:
        t.moderation_reason = t.moderation_reason.strip() or None
    if t.room_id:
        t.room_id = t.room_id.strip() or None
    if t.idempotency_key:
        t.idempotency_key = t.idempotency_key.strip() or None
    if t.request_id:
        t.request_id = t.request_id.strip() or None

@listens_for(Message, "before_update")
def _msg_before_update(_mapper, _conn, t: Message) -> None:
    # ensure content_hash stays in sync if content changed (outside .edit())
    if t.content:
        t.content = t.content.strip()
        if not t.content_hash:
            t.content_hash = _hash_text(t.content)
    if not t.thread_id:
        t.thread_id = t.parent_id or t.id
    if t.moderation_reason:
        t.moderation_reason = t.moderation_reason.strip() or None
    if t.room_id:
        t.room_id = t.room_id.strip() or None
    if t.idempotency_key:
        t.idempotency_key = t.idempotency_key.strip() or None
    if t.request_id:
        t.request_id = t.request_id.strip() or None
