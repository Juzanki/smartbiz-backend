# backend/models/chat.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, backref
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

if TYPE_CHECKING:
    from .user import User
    # from .live_session import LiveSession  # kama upo model huu, acha import ya runtime ifanyike kawaida

# --------- Enums ---------
class MessageType(str, enum.Enum):
    text = "text"
    sticker = "sticker"
    gif = "gif"
    image = "image"
    video = "video"
    system = "system"
    superchat = "superchat"   # paid highlight
    reaction = "reaction"     # e.g., emoji reaction

class MessageStatus(str, enum.Enum):
    sent = "sent"
    delivered = "delivered"
    read = "read"
    failed = "failed"
    moderated = "moderated"   # hidden/edited by mod

# --------- Model ---------
class ChatMessage(Base):
    """
    Ujumbe mmoja kwenye chumba cha mazungumzo (live).
    Ina threading (reply_to), moderation, attachments, na delivery states.
    """
    __tablename__ = "chat_messages"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Who
    sender_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Where
    room_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)  # ext. room/session id
    live_session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("live_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )

    # What
    message_type: Mapped[MessageType] = mapped_column(
        SQLEnum(MessageType, name="chat_message_type", native_enum=False, validate_strings=True),
        default=MessageType.text,
        nullable=False,
        index=True,
    )
    status: Mapped[MessageStatus] = mapped_column(
        SQLEnum(MessageStatus, name="chat_message_status", native_enum=False, validate_strings=True),
        default=MessageStatus.sent,
        nullable=False,
        index=True,
    )

    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Content/metadata nyepesi
    attachment_url: Mapped[Optional[str]] = mapped_column(String(500))
    thumbnail_url: Mapped[Optional[str]] = mapped_column(String(500))
    # {gift_id, amount, reply_excerpt, ...}
    meta: Mapped[Optional[dict]] = mapped_column(JSON_VARIANT)

    # Threading (reply to another ChatMessage)
    reply_to_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        index=True,
    )

    # Moderation / visibility
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    hidden_reason: Mapped[Optional[str]] = mapped_column(String(160))

    # Lifecycle
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    delivered_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    edited_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Soft delete
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # --------- Relationships ---------
    sender: Mapped["User"] = relationship(
        "User",
        backref=backref("chat_messages", lazy="selectin", cascade="all, delete-orphan"),
        foreign_keys=lambda: [ChatMessage.sender_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # Self-referential reply chain
    reply_to: Mapped[Optional["ChatMessage"]] = relationship(
        "ChatMessage",
        remote_side=lambda: [ChatMessage.id],
        foreign_keys=lambda: [ChatMessage.reply_to_id],
        backref=backref("replies", lazy="selectin", cascade="all, delete-orphan"),
        post_update=False,
        lazy="selectin",
    )

    # --------- Hybrids / Helpers ---------
    @hybrid_property
    def is_visible(self) -> bool:
        return (not self.is_deleted) and (not self.hidden)

    @hybrid_property
    def is_system(self) -> bool:
        return self.message_type == MessageType.system

    def mark_delivered(self) -> None:
        self.status = MessageStatus.delivered
        self.delivered_at = dt.datetime.now(dt.timezone.utc)

    def mark_read(self) -> None:
        self.status = MessageStatus.read
        self.read_at = dt.datetime.now(dt.timezone.utc)

    def edit_message(self, new_text: str) -> None:
        self.message = new_text
        self.edited_at = dt.datetime.now(dt.timezone.utc)

    def soft_delete(self, reason: str | None = None) -> None:
        self.is_deleted = True
        self.deleted_at = dt.datetime.now(dt.timezone.utc)
        self.hidden = True
        if reason and not self.hidden_reason:
            self.hidden_reason = reason

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ChatMessage id={self.id} sender={self.sender_id} "
            f"type={self.message_type} status={self.status} room={self.room_id}>"
        )

    # --------- Constraints / Indexes ---------
    __table_args__ = (
        CheckConstraint("length(message) >= 1", name="ck_chat_message_len_min"),
        Index("ix_chat_room_created", "room_id", "created_at"),
        Index("ix_chat_session_created", "live_session_id", "created_at"),
        Index("ix_chat_sender_created", "sender_id", "created_at"),
        Index("ix_chat_flags", "flagged", "hidden", "is_deleted"),
    )
