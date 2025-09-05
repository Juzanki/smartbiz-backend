# backend/models/customer_feedback.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, TYPE_CHECKING, List, Dict, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableList, MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB; others: JSON

if TYPE_CHECKING:
    from .user import User
    from .customer import Customer


# ---------- Enums ----------
class FeedbackType(str, enum.Enum):
    complaint  = "complaint"
    praise     = "praise"
    suggestion = "suggestion"
    question   = "question"
    general    = "general"


class FeedbackSource(str, enum.Enum):
    web      = "web"
    email    = "email"
    sms      = "sms"
    whatsapp = "whatsapp"
    app      = "app"
    other    = "other"


class FeedbackStatus(str, enum.Enum):
    new          = "new"
    acknowledged = "acknowledged"
    in_progress  = "in_progress"
    resolved     = "resolved"
    closed       = "closed"


class Sentiment(str, enum.Enum):
    negative = "negative"
    neutral  = "neutral"
    positive = "positive"
    mixed    = "mixed"
    unknown  = "unknown"


class CustomerFeedback(Base):
    """
    A customer's feedback owned by a business user (user_id).
    Optional link to CRM Customer; tracks lifecycle/SLA/sentiment/attachments.
    """
    __tablename__ = "customer_feedbacks"
    __mapper_args__ = {"eager_defaults": True}

    # --- Identity ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Owner (business)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Optional CRM linkage
    customer_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), index=True, default=None
    )

    # Free-form identity if no customer_id
    customer_name: Mapped[Optional[str]] = mapped_column(String(120))
    contact: Mapped[Optional[str]] = mapped_column(String(160))  # email/phone/handle

    # Classification
    feedback_type: Mapped[FeedbackType] = mapped_column(
        SQLEnum(FeedbackType, name="feedback_type", native_enum=False, validate_strings=True),
        default=FeedbackType.general, nullable=False, index=True,
    )
    source: Mapped[FeedbackSource] = mapped_column(
        SQLEnum(FeedbackSource, name="feedback_source", native_enum=False, validate_strings=True),
        default=FeedbackSource.web, nullable=False, index=True,
    )
    status: Mapped[FeedbackStatus] = mapped_column(
        SQLEnum(FeedbackStatus, name="feedback_status", native_enum=False, validate_strings=True),
        default=FeedbackStatus.new, nullable=False, index=True,
    )

    # Content
    subject: Mapped[Optional[str]] = mapped_column(String(160))
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Rating & sentiment
    rating: Mapped[Optional[int]] = mapped_column(Integer)  # 1..5 optional
    sentiment: Mapped[Sentiment] = mapped_column(
        SQLEnum(Sentiment, name="feedback_sentiment", native_enum=False, validate_strings=True),
        default=Sentiment.unknown, nullable=False, index=True,
    )

    # Attachments & metadata (mutable for in-place changes)
    attachments: Mapped[Optional[List[Any]]] = mapped_column(
        MutableList.as_mutable(JSON_VARIANT)
    )  # e.g. ["url"] or [{"url":..., "name":...}]
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        MutableDict.as_mutable(JSON_VARIANT)
    )  # {"device":"...", "locale":"..."} etc.

    # SLA / response tracking
    sla_due_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    first_response_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Assignee (agent/staff)
    assigned_to_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # Timestamps
    submitted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    # --- Relationships ---
    user: Mapped["User"] = relationship(
        "User",
        back_populates="customer_feedbacks",
        foreign_keys=[user_id],
        passive_deletes=True,
        lazy="selectin",
    )
    assignee: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="feedbacks_assigned",
        foreign_keys=[assigned_to_user_id],
        lazy="selectin",
    )
    customer: Mapped[Optional["Customer"]] = relationship(
        "Customer",
        back_populates="customer_feedbacks",
        foreign_keys=[customer_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # --- Hybrids / helpers ---
    @hybrid_property
    def is_overdue(self) -> bool:
        if not self.sla_due_at:
            return False
        return (dt.datetime.now(dt.timezone.utc) > self.sla_due_at) and \
               (self.status not in (FeedbackStatus.resolved, FeedbackStatus.closed))

    @hybrid_property
    def is_open(self) -> bool:
        return self.status not in (FeedbackStatus.resolved, FeedbackStatus.closed)

    @hybrid_property
    def response_time_seconds(self) -> Optional[int]:
        if self.first_response_at is None:
            return None
        return int((self.first_response_at - self.submitted_at).total_seconds())

    @hybrid_property
    def resolution_time_seconds(self) -> Optional[int]:
        if self.resolved_at is None:
            return None
        return int((self.resolved_at - self.submitted_at).total_seconds())

    # --- State transitions ---
    def acknowledge(self) -> None:
        if self.status == FeedbackStatus.new:
            self.status = FeedbackStatus.acknowledged
            if not self.first_response_at:
                self.first_response_at = dt.datetime.now(dt.timezone.utc)

    def start_progress(self) -> None:
        self.status = FeedbackStatus.in_progress
        if not self.first_response_at:
            self.first_response_at = dt.datetime.now(dt.timezone.utc)

    def resolve(self) -> None:
        self.status = FeedbackStatus.resolved
        self.resolved_at = dt.datetime.now(dt.timezone.utc)

    def close(self) -> None:
        self.status = FeedbackStatus.closed
        self.closed_at = dt.datetime.now(dt.timezone.utc)

    def set_sentiment(self, value: str | Sentiment) -> None:
        if isinstance(value, Sentiment):
            self.sentiment = value
            return
        v = (value or "").strip().lower()
        self.sentiment = {
            "negative": Sentiment.negative,
            "neutral": Sentiment.neutral,
            "positive": Sentiment.positive,
            "mixed": Sentiment.mixed,
        }.get(v, Sentiment.unknown)

    # --- Validators ---
    @validates("rating")
    def _validate_rating(self, _k, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        iv = int(v)
        if not (1 <= iv <= 5):
            raise ValueError("rating must be between 1 and 5")
        return iv

    @validates("subject", "customer_name", "contact")
    def _trim_strings(self, _k, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        t = v.strip()
        return t or None

    # --- Repr ---
    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<CustomerFeedback id={self.id} user={self.user_id} "
            f"type={self.feedback_type} status={self.status} rating={self.rating}>"
        )

    # --- Constraints & Indexes ---
    __table_args__ = (
        Index("ix_cf_user_created", "user_id", "submitted_at"),
        Index("ix_cf_type_status", "feedback_type", "status"),
        Index("ix_cf_sentiment_rating", "sentiment", "rating"),
        Index("ix_cf_customer", "customer_id"),
        CheckConstraint("rating IS NULL OR (rating BETWEEN 1 AND 5)", name="ck_cf_rating_range"),
        CheckConstraint("length(message) >= 2", name="ck_cf_message_len"),
    )
