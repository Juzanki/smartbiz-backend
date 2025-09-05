# backend/models/smart_reply.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import hashlib
import datetime as dt
from typing import Optional, Dict, Any, List, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
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
    from .chat_message import ChatMessage
    from .user_bot import UserBot  # kama huna, acha TYPE_CHECKING ili isi-lazimike wakati wa runtime

# ----------------- Enums (salama kwa DB + validate strings) -----------------

class ReplyStatus(str, enum.Enum):
    suggested = "suggested"
    shown     = "shown"
    sent      = "sent"
    clicked   = "clicked"
    accepted  = "accepted"
    dismissed = "dismissed"
    expired   = "expired"
    failed    = "failed"

class ReplyChannel(str, enum.Enum):
    app = "app"
    web = "web"
    android = "android"
    ios = "ios"
    whatsapp = "whatsapp"
    telegram = "telegram"
    sms = "sms"
    other = "other"

class ReplyTrigger(str, enum.Enum):
    incoming_message = "incoming_message"
    system  = "system"
    manual  = "manual"
    schedule = "schedule"
    api     = "api"

class ReplyVisibility(str, enum.Enum):
    public  = "public"
    private = "private"


# ------------------------------ Model ---------------------------------------

class SmartReply(Base):
    """
    SmartReply — AI-generated quick replies with analytics (mobile-first, scalable).
    """
    __tablename__ = "smart_replies"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        # Dedupe ya “slot” moja (context) kwa cheo husika
        UniqueConstraint("context_hash", "rank", name="uq_smart_reply_context_rank"),
        UniqueConstraint("idempotency_key", name="uq_smart_reply_idem"),
        # Hot paths
        Index("ix_smart_reply_room_time", "room_id", "created_at"),
        Index("ix_smart_reply_status_time", "status", "created_at"),
        Index("ix_smart_reply_clicked", "room_id", "status"),
        Index("ix_smart_reply_context", "context_hash"),
        Index("ix_smart_reply_room_vis_rank", "room_id", "visibility", "status", "rank"),
        Index("ix_smart_reply_expires", "expires_at"),
        Index("ix_smart_reply_user_time", "user_id", "created_at"),
        # Guards
        CheckConstraint("length(message) > 0", name="ck_smart_reply_message_nonempty"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="ck_smart_reply_confidence_bounds"),
        CheckConstraint("relevance  >= 0.0 AND relevance  <= 1.0", name="ck_smart_reply_relevance_bounds"),
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="ck_smart_reply_tokens_nonneg"),
        CheckConstraint("amount_minor IS NULL OR amount_minor >= 0", name="ck_smart_reply_cost_nonneg"),
        CheckConstraint("currency IS NULL OR length(currency) BETWEEN 2 AND 8", name="ck_smart_reply_currency_len"),
        CheckConstraint("(expires_at IS NULL) OR (created_at IS NULL) OR (expires_at >= created_at)",
                        name="ck_smart_reply_expiry_after_create"),
    )

    # Identity
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Context
    room_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    visibility: Mapped[ReplyVisibility] = mapped_column(
        SQLEnum(ReplyVisibility, name="smart_reply_visibility", native_enum=False, validate_strings=True),
        default=ReplyVisibility.public,
        nullable=False,
        index=True,
    )

    # Optional linkage to the message that triggered the suggestion
    message_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )

    # Who (recipient/owner of suggestion) & optional bot/persona
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )
    bot_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user_bots.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )

    # Generated content
    message: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[Optional[str]] = mapped_column(String(64))
    language: Mapped[str] = mapped_column(String(12), default="en", nullable=False, index=True)
    tone: Mapped[Optional[str]] = mapped_column(String(24))
    suggestions: Mapped[Optional[List[str]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Model & safety
    model: Mapped[Optional[str]] = mapped_column(String(64))
    model_version: Mapped[Optional[str]] = mapped_column(String(32))
    safety_labels: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=text("false"), index=True)
    block_reason: Mapped[Optional[str]] = mapped_column(String(64))

    # Relevance & ranking
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    relevance:  Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    rank:       Mapped[int]   = mapped_column(Integer, default=0, nullable=False)

    # Delivery & lifecycle
    channel: Mapped[ReplyChannel] = mapped_column(
        SQLEnum(ReplyChannel, name="smart_reply_channel", native_enum=False, validate_strings=True),
        default=ReplyChannel.app,
        nullable=False,
        index=True,
    )
    trigger: Mapped[ReplyTrigger] = mapped_column(
        SQLEnum(ReplyTrigger, name="smart_reply_trigger", native_enum=False, validate_strings=True),
        default=ReplyTrigger.incoming_message,
        nullable=False,
        index=True,
    )
    status: Mapped[ReplyStatus] = mapped_column(
        SQLEnum(ReplyStatus, name="smart_reply_status", native_enum=False, validate_strings=True),
        default=ReplyStatus.suggested,
        nullable=False,
        index=True,
    )

    shown_at:     Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    clicked_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    sent_at:      Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    accepted_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    expires_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Simple impression/click counters
    impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicks:      Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Accounting (tokens/cost)
    input_tokens:  Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    amount_minor:  Mapped[Optional[int]] = mapped_column(Integer)
    currency:      Mapped[Optional[str]] = mapped_column(String(8))

    # Feedback / moderation signals
    thumbs_up:    Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    thumbs_down:  Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    feedback_note: Mapped[Optional[str]] = mapped_column(String(255))

    # Grouping & idempotency
    context_hash:   Mapped[str] = mapped_column(String(64), index=True, nullable=False,
                         doc="sha256 over (room|message_id|intent|language|model)")
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    meta:            Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships (optional; keep lazy to avoid heavy joins)
    user:         Mapped[Optional["User"]]        = relationship("User", lazy="selectin")
    bot:          Mapped[Optional["UserBot"]]     = relationship("UserBot", lazy="selectin")
    message_ref:  Mapped[Optional["ChatMessage"]] = relationship("ChatMessage", lazy="selectin")

    # ----------------------------- Hybrids ---------------------------------

    @hybrid_property
    def is_terminal(self) -> bool:
        return self.status in {ReplyStatus.sent, ReplyStatus.accepted, ReplyStatus.dismissed,
                               ReplyStatus.expired, ReplyStatus.failed}

    @hybrid_property
    def is_active(self) -> bool:
        if self.blocked:
            return False
        if self.expires_at and dt.datetime.now(dt.timezone.utc) >= self.expires_at:
            return False
        return self.status in {ReplyStatus.suggested, ReplyStatus.shown, ReplyStatus.clicked}

    @hybrid_property
    def score(self) -> float:
        return float(0.5 * (self.confidence or 0) + 0.5 * (self.relevance or 0))

    @hybrid_property
    def ctr(self) -> float:
        return 0.0 if self.impressions <= 0 else float(self.clicks) / float(self.impressions)

    # ----------------------------- Helpers ---------------------------------

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def compute_context_hash(self) -> None:
        basis = f"{self.room_id}|{self.message_id or ''}|{self.intent or ''}|{self.language or ''}|{self.model or ''}"
        self.context_hash = self._sha256(basis)

    # Lifecycle transitions
    def mark_shown(self) -> None:
        if self.status == ReplyStatus.suggested:
            self.status = ReplyStatus.shown
        self.shown_at = self.shown_at or dt.datetime.now(dt.timezone.utc)
        self.impressions += 1

    def mark_clicked(self) -> None:
        self.status = ReplyStatus.clicked
        self.clicked_at = dt.datetime.now(dt.timezone.utc)
        self.clicks += 1

    def mark_sent(self) -> None:
        self.status = ReplyStatus.sent
        self.sent_at = dt.datetime.now(dt.timezone.utc)

    def mark_accepted(self) -> None:
        self.status = ReplyStatus.accepted
        self.accepted_at = dt.datetime.now(dt.timezone.utc)

    def mark_dismissed(self) -> None:
        self.status = ReplyStatus.dismissed
        self.dismissed_at = dt.datetime.now(dt.timezone.utc)

    def mark_failed(self, reason: Optional[str] = None) -> None:
        self.status = ReplyStatus.failed
        self.block_reason = (reason or None)

    def expire(self) -> None:
        self.status = ReplyStatus.expired
        self.expires_at = self.expires_at or dt.datetime.now(dt.timezone.utc)

    # Tokens & cost
    def set_tokens(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens = max(0, int(input_tokens))
        self.output_tokens = max(0, int(output_tokens))

    def set_cost_minor(self, *, amount_minor: Optional[int], currency: Optional[str]) -> None:
        if amount_minor is not None and amount_minor < 0:
            raise ValueError("amount_minor must be >= 0")
        self.amount_minor = amount_minor
        self.currency = (currency or None)[:8] if currency else None

    # Feedback
    def upvote(self, n: int = 1) -> None:
        self.thumbs_up += max(0, int(n))

    def downvote(self, n: int = 1) -> None:
        self.thumbs_down += max(0, int(n))

    def record_feedback(self, note: Optional[str]) -> None:
        self.feedback_note = (note or "")[:255] or None

    # TTL helpers
    def ensure_ttl(self, ttl_seconds: Optional[int]) -> None:
        if ttl_seconds and not self.expires_at:
            base = self.created_at or dt.datetime.now(dt.timezone.utc)
            self.expires_at = base + dt.timedelta(seconds=int(max(1, ttl_seconds)))

    # Public projection (mobile-friendly)
    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "room_id": self.room_id,
            "message_id": self.message_id,
            "user_id": self.user_id,
            "bot_id": self.bot_id,
            "message": self.message,
            "suggestions": list(self.suggestions or []),
            "intent": self.intent,
            "language": self.language,
            "tone": self.tone,
            "confidence": round(float(self.confidence or 0.0), 4),
            "relevance": round(float(self.relevance or 0.0), 4),
            "score": round(self.score, 4),
            "rank": self.rank,
            "status": self.status.value if isinstance(self.status, ReplyStatus) else self.status,
            "visibility": self.visibility.value if isinstance(self.visibility, ReplyVisibility) else self.visibility,
            "channel": self.channel.value if isinstance(self.channel, ReplyChannel) else self.channel,
            "trigger": self.trigger.value if isinstance(self.trigger, ReplyTrigger) else self.trigger,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "metrics": {
                "impressions": self.impressions,
                "clicks": self.clicks,
                "ctr": round(self.ctr, 4),
                "thumbs_up": self.thumbs_up,
                "thumbs_down": self.thumbs_down,
            },
            "timestamps": {
                "created_at": self.created_at.isoformat() if self.created_at else None,
                "shown_at": self.shown_at.isoformat() if self.shown_at else None,
                "clicked_at": self.clicked_at.isoformat() if self.clicked_at else None,
                "sent_at": self.sent_at.isoformat() if self.sent_at else None,
                "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None,
                "dismissed_at": self.dismissed_at.isoformat() if self.dismissed_at else None,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            },
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<SmartReply id={self.id} room={self.room_id} status={self.status} "
            f"rank={self.rank} conf={self.confidence:.2f} rel={self.relevance:.2f}>"
        )
