# backend/models/password_reset_code.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import os
import hmac
import secrets
import hashlib
import datetime as dt
from typing import Optional, Dict, Any, List, TYPE_CHECKING

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
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    from .forgot_password import ForgotPasswordRequest


# ---------------- Enums ----------------
class ResetPurpose(str, enum.Enum):
    password_reset  = "password_reset"
    email_verify    = "email_verify"
    login_challenge = "login_challenge"
    change_email    = "change_email"
    other           = "other"


class ResetStatus(str, enum.Enum):
    pending = "pending"
    used    = "used"
    expired = "expired"
    revoked = "revoked"


class DeliveryChannel(str, enum.Enum):
    email  = "email"
    sms    = "sms"
    push   = "push"
    call   = "call"
    inapp  = "inapp"
    other  = "other"


class CodeFormat(str, enum.Enum):
    numeric       = "numeric"
    alphanumeric  = "alphanumeric"


# ---------------- Helpers ----------------
def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------- Model ----------------
class PasswordResetCode(Base):
    """
    OTP salama (audit-grade):
      - Hifadhi HASH ya code pekee (+ optional pepper)
      - Throttling: attempts per-code na per-window
      - Resend cooldown & counters
      - Delivery metadata (channel, provider ids)
      - Lifecycle helpers: verify/consume/revoke/extend/mark_sent
      - JSON meta ni mutable (tracked)
    """
    __tablename__ = "password_reset_codes"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Owner (moja kati ya user_id au email lazima iwepo)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    user: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[user_id], lazy="selectin", passive_deletes=True
    )

    email: Mapped[Optional[str]] = mapped_column(String(255))
    email_lower: Mapped[Optional[str]] = mapped_column(String(255), index=True)

    # Classification
    purpose: Mapped[ResetPurpose] = mapped_column(
        SQLEnum(ResetPurpose, name="reset_purpose", native_enum=False, validate_strings=True),
        default=ResetPurpose.password_reset, nullable=False, index=True
    )
    status: Mapped[ResetStatus] = mapped_column(
        SQLEnum(ResetStatus, name="reset_status", native_enum=False, validate_strings=True),
        default=ResetStatus.pending, nullable=False, index=True
    )

    # Code (hash-only)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    code_hint: Mapped[Optional[str]] = mapped_column(String(12))  # kionjo kidogo kwa logs
    code_format: Mapped[CodeFormat] = mapped_column(
        SQLEnum(CodeFormat, name="reset_code_format", native_enum=False, validate_strings=True),
        default=CodeFormat.numeric, nullable=False, index=True
    )
    code_length: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("6"))

    # Attempts / throttling
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    last_attempt_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Sliding window throttling (per code)
    window_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    window_started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("300"))  # 5 min
    window_max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))

    # Resend controls
    resend_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Delivery metadata
    channel: Mapped[DeliveryChannel] = mapped_column(
        SQLEnum(DeliveryChannel, name="reset_delivery_channel", native_enum=False, validate_strings=True),
        default=DeliveryChannel.email, nullable=False, index=True
    )
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    provider: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # Context
    requested_ip: Mapped[Optional[str]] = mapped_column(String(64))
    requested_ua: Mapped[Optional[str]] = mapped_column(String(400))
    consumed_ip: Mapped[Optional[str]] = mapped_column(String(64))
    consumed_ua: Mapped[Optional[str]] = mapped_column(String(400))
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Correlation / dedupe
    request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used_at:     Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    revoked_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    invalidated_reason: Mapped[Optional[str]] = mapped_column(String(160))

    # ---------------- Relationships (bi-directional) ----------------
    forgot_password_requests: Mapped[List["ForgotPasswordRequest"]] = relationship(
        "ForgotPasswordRequest",
        back_populates="reset_code",
        passive_deletes=True,
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def is_expired(self) -> bool:
        return bool(self.expires_at and _utcnow() >= self.expires_at)

    @hybrid_property
    def attempts_left(self) -> int:
        return max(0, (self.max_attempts or 0) - (self.attempt_count or 0))

    @hybrid_property
    def consumable(self) -> bool:
        return self.status == ResetStatus.pending and not self.is_expired and self.attempts_left > 0

    @hybrid_property
    def remaining_seconds(self) -> int:
        if not self.expires_at:
            return 0
        return max(0, int((self.expires_at - _utcnow()).total_seconds()))

    @hybrid_property
    def can_resend(self) -> bool:
        return not self.cooldown_until or _utcnow() >= self.cooldown_until

    # ---------- Code helpers ----------
    @staticmethod
    def _pepper() -> str:
        return os.getenv("PRC_PEPPER", "")

    @classmethod
    def hash_code(cls, plain: str) -> str:
        return _sha256((plain or "") + cls._pepper())

    @staticmethod
    def _mask_hint(plain: str) -> str:
        p = plain or ""
        if len(p) <= 2:
            return p[:1] + "…"
        if len(p) <= 4:
            return p[:2] + "…" + p[-1:]
        return p[:2] + "…" + p[-2:]

    def set_code_from_plain(self, plain: str) -> None:
        self.code_hash = self.hash_code(plain)
        self.code_hint = self._mask_hint(plain)

    def generate_code(self, *, digits: int = 6, fmt: CodeFormat | str = CodeFormat.numeric) -> str:
        fmt = CodeFormat(fmt)
        digits = max(4, min(10, int(digits)))
        self.code_length = digits
        if fmt == CodeFormat.numeric:
            code = str(secrets.randbelow(10 ** digits)).zfill(digits)
        else:
            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # epuka 0/O/I/1
            code = "".join(secrets.choice(alphabet) for _ in range(digits))
        self.code_format = fmt
        self.set_code_from_plain(code)
        return code

    # ---------- Flow helpers ----------
    def _within_window(self) -> bool:
        if not self.window_started_at:
            return False
        return (_utcnow() - self.window_started_at) <= dt.timedelta(seconds=int(self.window_seconds or 0))

    def _bump_window(self) -> None:
        now = _utcnow()
        if not self._within_window():
            self.window_started_at = now
            self.window_attempts = 0
        self.window_attempts = int(self.window_attempts or 0) + 1

    def _window_allowed(self) -> bool:
        if not self._within_window():
            return True
        return int(self.window_attempts or 0) < int(self.window_max_attempts or 0)

    def schedule_cooldown(self, seconds: int = 60) -> None:
        self.cooldown_until = _utcnow() + dt.timedelta(seconds=max(5, int(seconds)))

    def mark_sent(self, *, provider_message_id: Optional[str] = None, provider: Optional[str] = None,
                  cooldown_seconds: int = 60) -> None:
        self.resend_count = (self.resend_count or 0) + 1
        self.last_sent_at = _utcnow()
        if provider_message_id:
            self.provider_message_id = provider_message_id
        if provider:
            self.provider = provider
        self.schedule_cooldown(cooldown_seconds)

    def extend_expiry(self, *, seconds: int) -> None:
        self.expires_at = (self.expires_at or _utcnow()) + dt.timedelta(seconds=max(1, int(seconds)))

    def can_attempt(self) -> bool:
        return self.status == ResetStatus.pending and not self.is_expired and self.attempts_left > 0 and self._window_allowed()

    def check_code(
        self, plain: str, *, consume_on_success: bool = True,
        ip: Optional[str] = None, ua: Optional[str] = None
    ) -> bool:
        self.last_attempt_at = _utcnow()

        # Expire lazily
        if self.is_expired and self.status == ResetStatus.pending:
            self.status = ResetStatus.expired
            self.invalidated_reason = self.invalidated_reason or "expired"
            return False

        if not self.can_attempt():
            return False

        self._bump_window()

        ok = hmac.compare_digest(self.code_hash, self.hash_code(plain))
        if ok:
            if consume_on_success:
                self.status = ResetStatus.used
                self.used_at = _utcnow()
                self.consumed_ip = ip
                self.consumed_ua = ua
            return True

        # failed attempt
        self.attempt_count = (self.attempt_count or 0) + 1
        if self.attempts_left <= 0 and self.status == ResetStatus.pending:
            self.status = ResetStatus.expired
            self.invalidated_reason = self.invalidated_reason or "max_attempts"
        return False

    def revoke(self, *, reason: str | None = None) -> None:
        self.status = ResetStatus.revoked
        self.revoked_at = _utcnow()
        if reason:
            self.invalidated_reason = reason
            self.meta = {**(self.meta or {}), "revoked_reason": reason}

    def normalize_email(self) -> None:
        if self.email:
            self.email_lower = (self.email or "").strip().lower() or None

    def __repr__(self) -> str:  # pragma: no cover
        owner = self.user_id or self.email_lower
        return f"<PasswordResetCode id={self.id} owner={owner} status={self.status} purpose={self.purpose} rem={self.remaining_seconds}s>"

    # -------- Indices & Constraints --------
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_prc_idem"),
        Index("ix_prc_owner_purpose_status", "user_id", "email_lower", "purpose", "status"),
        Index("ix_prc_user_created", "user_id", "created_at"),
        Index("ix_prc_email_created", "email_lower", "created_at"),
        Index("ix_prc_status_expiry", "status", "expires_at"),
        Index("ix_prc_request", "request_id"),
        Index("ix_prc_cooldown", "cooldown_until"),
        CheckConstraint("(user_id IS NOT NULL) OR (email_lower IS NOT NULL)", name="ck_prc_owner_present"),
        CheckConstraint("attempt_count >= 0", name="ck_prc_attempts_nonneg"),
        CheckConstraint("max_attempts >= 1", name="ck_prc_max_attempts_min1"),
        CheckConstraint("window_seconds >= 1 AND window_max_attempts >= 1", name="ck_prc_window_positive"),
        CheckConstraint("resend_count >= 0", name="ck_prc_resend_nonneg"),
        CheckConstraint("code_length >= 4 AND code_length <= 10", name="ck_prc_code_len_4_10"),
        CheckConstraint("expires_at > created_at", name="ck_prc_expiry_after_create"),
        {"extend_existing": True},
    )


# ---------------- Validators / Normalizers ----------------
@validates("email")
def _validate_email(_inst, _key, value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip()
    return v or None


@validates("email_lower")
def _normalize_email_lower(_inst, _key, value: Optional[str]) -> Optional[str]:
    return (value or "").strip().lower() or None


@validates("provider_message_id", "provider", "request_id", "invalidated_reason")
def _trim_texts(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = value.strip()
    return s or None


# ---------------- Hooks ----------------
@listens_for(PasswordResetCode, "before_insert")
def _prc_before_insert(_m, _c, t: PasswordResetCode) -> None:  # pragma: no cover
    t.normalize_email()
    if (t.window_seconds or 0) > 0 and not t.window_started_at:
        t.window_started_at = _utcnow()


@listens_for(PasswordResetCode, "before_update")
def _prc_before_update(_m, _c, t: PasswordResetCode) -> None:  # pragma: no cover
    t.normalize_email()
