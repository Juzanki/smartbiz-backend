# backend/models/token_usage_log.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import hashlib
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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

_ALLOWED_TYPES = (
    "access",        # short-lived access token
    "refresh",       # refresh token
    "magic_link",    # one-time sign-in link
    "api_key",       # long-lived API key
    "password_reset",
    "email_verify",
    "other",
)

class TokenUsageLog(Base):
    """
    TokenUsageLog — audit trail for token/key usage (security-first, mobile-friendly).

    Upgrades:
    - SQLAlchemy 2.0 typed mappings
    - Timezone-aware timestamps
    - Outcome fields (was_valid, reason, status_code)
    - Rich context (token_id/jti, ua_hash, geo, provider, metadata)
    - Strong constraints + helpful indexes
    - Helpers to record success/failure and normalize UA/IP
    """
    __tablename__ = "token_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Who
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # What
    token_type: Mapped[str] = mapped_column(String(32), nullable=False)      # see _ALLOWED_TYPES
    token_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)  # jti, kid, or key id if available
    provider: Mapped[Optional[str]] = mapped_column(String(32), default=None)  # e.g., "auth0", "internal"

    # Where/Device
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), default=None, index=True)
    device_info: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    ua_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True, default=None)  # sha256(user_agent)
    country: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    city: Mapped[Optional[str]] = mapped_column(String(64), default=None)

    # When
    used_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    token_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)

    # Outcome
    was_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(
        String(64), default=None, doc="why invalid: expired|revoked|mismatch|ip_blocked|rate_limited|other"
    )
    status_code: Mapped[Optional[int]] = mapped_column(Integer, default=None)  # http-ish status if applicable

    # Extra payload
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="token_usage_logs", lazy="selectin")

    __table_args__ = (
        CheckConstraint("length(token_type) > 0", name="ck_token_usage_type_nonempty"),
        CheckConstraint("status_code IS NULL OR (status_code >= 100 AND status_code <= 599)",
                        name="ck_token_usage_status_range"),
        Index("ix_token_usage_user_time", "user_id", "used_at"),
        Index("ix_token_usage_type_time", "token_type", "used_at"),
        Index("ix_token_usage_tokenid_time", "token_id", "used_at"),
        Index("ix_token_usage_ip_time", "ip_address", "used_at"),
    )

    # ------------------------ Helpers (no DB I/O here) ------------------------ #
    @staticmethod
    def _sha256(text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def normalize_context(
        self,
        *,
        ip: Optional[str] = None,
        ua: Optional[str] = None,
        device: Optional[str] = None,
        country: Optional[str] = None,
        city: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> None:
        """Set/normalize network & device context."""
        if ip is not None:
            self.ip_address = ip[:64] if ip else None
        if ua is not None:
            self.user_agent = ua[:255] if ua else None
            self.ua_hash = self._sha256(ua)
        if device is not None:
            self.device_info = device[:255] if device else None
        if country is not None:
            self.country = country[:64] if country else None
        if city is not None:
            self.city = city[:64] if city else None
        if provider is not None:
            self.provider = provider[:32] if provider else None

    def mark_success(self, *, status: Optional[int] = None, meta: Optional[Dict[str, Any]] = None) -> None:
        """Record a successful token usage."""
        self.was_valid = True
        self.reason = None
        if status is not None:
            self.status_code = int(status)
        if meta is not None:
            self.meta = {**(self.meta or {}), **meta}

    def mark_failure(
        self,
        *,
        reason: str = "other",
        status: Optional[int] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a failed token usage (expired/revoked/etc.)."""
        self.was_valid = False
        self.reason = (reason or "other")[:64]
        if status is not None:
            self.status_code = int(status)
        if meta is not None:
            self.meta = {**(self.meta or {}), **meta}

    def set_token(self, *, token_type: str, token_id: Optional[str] = None, expires_at: Optional[dt.datetime] = None) -> None:
        """Attach token identity & expiry snapshot."""
        t = (token_type or "other").lower()
        # allow any value but keep a consistent small set in your application layer
        self.token_type = t[:32]
        self.token_id = token_id[:64] if token_id else None
        self.token_expires_at = expires_at

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<TokenUsageLog id={self.id} user={self.user_id} type={self.token_type} "
            f"valid={self.was_valid} used_at={self.used_at}>"
        )




