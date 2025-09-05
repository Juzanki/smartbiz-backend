# backend/models/forgot_password.py
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
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

if TYPE_CHECKING:
    from .user import User
    # Hii ni hiari – kama una model PasswordResetCode, itajumuika hapa:
    from .password_reset_code import PasswordResetCode


# ---------- Enums ----------
# Jaribu kureuse kutoka password_reset_code, ukikosa tumia fallback hapa
try:  # pragma: no cover
    from .password_reset_code import ResetChannel, ResetPurpose  # type: ignore
except Exception:  # pragma: no cover
    class ResetChannel(str, enum.Enum):
        email = "email"
        sms = "sms"
        whatsapp = "whatsapp"
        other = "other"

    class ResetPurpose(str, enum.Enum):
        password = "password"
        email_verify = "email_verify"
        mfa = "mfa"
        unlock = "unlock"


class RequestStatus(str, enum.Enum):
    requested  = "requested"
    sent       = "sent"
    delivered  = "delivered"
    verified   = "verified"
    used       = "used"
    expired    = "expired"
    failed     = "failed"
    cancelled  = "cancelled"


# ---------- Helpers ----------
def _normalize_email(email: str | None) -> str | None:
    return (email or "").strip().lower() or None

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class ForgotPasswordRequest(Base):
    """
    Ombi la 'Forgot password'. Code halihifadhiwi hapa (lipo PasswordResetCode).
    Inatunza status, channel, rate-limit na meta za ombi.
    """
    __tablename__ = "forgot_password_requests"
    __mapper_args__ = {"eager_defaults": True}

    # Keys
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    reset_code_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("password_reset_codes.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # Identity
    email: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    email_lower: Mapped[Optional[str]] = mapped_column(String(255), index=True)

    # Classification
    purpose: Mapped[ResetPurpose] = mapped_column(
        SQLEnum(ResetPurpose, name="fpr_purpose", native_enum=False, validate_strings=True),
        default=ResetPurpose.password,
        nullable=False,
        index=True,
    )
    channel: Mapped[ResetChannel] = mapped_column(
        SQLEnum(ResetChannel, name="fpr_channel", native_enum=False, validate_strings=True),
        default=ResetChannel.email,
        nullable=False,
        index=True,
    )
    status: Mapped[RequestStatus] = mapped_column(
        SQLEnum(RequestStatus, name="fpr_status", native_enum=False, validate_strings=True),
        default=RequestStatus.requested,
        nullable=False,
        index=True,
    )

    # Timing
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"), nullable=False
    )
    sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Rate-limit / audit
    send_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_send_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    request_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    client_ip: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(String(400))
    meta: Mapped[Optional[dict]] = mapped_column(JSON_VARIANT)

    # Relationships
    user: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[user_id],
        back_populates="forgot_password_requests",
        passive_deletes=True,
        lazy="selectin",
    )
    reset_code: Mapped[Optional["PasswordResetCode"]] = relationship(
        "PasswordResetCode",
        foreign_keys=[reset_code_id],
        back_populates="forgot_password_requests",
        passive_deletes=True,
        lazy="selectin",
    )

    # Hybrids / helpers
    @hybrid_property
    def is_open(self) -> bool:
        return self.status in {
            RequestStatus.requested, RequestStatus.sent, RequestStatus.delivered, RequestStatus.verified
        }

    @hybrid_property
    def is_expired(self) -> bool:
        return bool(self.expires_at and _utcnow() >= self.expires_at)

    @hybrid_property
    def remaining_attempts(self) -> int:
        return max(0, (self.max_send_attempts or 0) - (self.send_attempts or 0))

    @hybrid_method
    def can_send(self) -> bool:
        return self.is_open and not self.is_expired and self.remaining_attempts > 0

    # State transitions
    def mark_sent(self) -> None:
        self.status = RequestStatus.sent
        self.sent_at = _utcnow()
        self.send_attempts = (self.send_attempts or 0) + 1

    def mark_delivered(self) -> None:
        self.status = RequestStatus.delivered
        self.delivered_at = _utcnow()

    def mark_verified(self) -> None:
        self.status = RequestStatus.verified
        self.verified_at = _utcnow()

    def mark_used(self) -> None:
        self.status = RequestStatus.used
        self.used_at = _utcnow()

    def mark_failed(self) -> None:
        self.status = RequestStatus.failed

    def mark_expired(self) -> None:
        self.status = RequestStatus.expired

    def cancel(self) -> None:
        self.status = RequestStatus.cancelled

    def set_email(self, value: Optional[str]) -> None:
        self.email = (value or "").strip() or None
        self.email_lower = _normalize_email(value)

    # Validators
    @validates("max_send_attempts")
    def _val_max_attempts(self, _k: str, v: int) -> int:
        iv = int(v)
        if iv < 1 or iv > 10:
            raise ValueError("max_send_attempts must be between 1 and 10")
        return iv

    @validates("send_attempts")
    def _val_attempts(self, _k: str, v: int) -> int:
        iv = int(v)
        if iv < 0:
            raise ValueError("send_attempts must be >= 0")
        return iv

    @validates("request_id")
    def _val_request_id(self, _k: str, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        return v.strip()[:64] or None

    @validates("client_ip")
    def _val_ip(self, _k: str, v: Optional[str]) -> Optional[str]:
        return None if not v else v.strip()[:64]

    @validates("user_agent")
    def _val_ua(self, _k: str, v: Optional[str]) -> Optional[str]:
        return None if not v else v.strip()[:400]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ForgotPasswordRequest id={self.id} email={self.email_lower} status={self.status} purpose={self.purpose}>"

    # Constraints & Indexes
    __table_args__ = (
        Index("ix_fpr_email_status", "email_lower", "status"),
        Index("ix_fpr_user_status", "user_id", "status"),
        Index("ix_fpr_created", "created_at"),
        UniqueConstraint("request_id", name="uq_fpr_request_id"),
        CheckConstraint(
            "send_attempts >= 0 AND send_attempts <= max_send_attempts",
            name="ck_fpr_send_attempts",
        ),
        CheckConstraint(
            "(expires_at IS NULL) OR (created_at IS NULL) OR (expires_at > created_at)",
            name="ck_fpr_expiry_after_create",
        ),
        CheckConstraint("length(email_lower) <= 255", name="ck_fpr_email_len"),
    )
