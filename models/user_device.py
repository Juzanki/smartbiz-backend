# backend/models/user_device.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import datetime as dt
from typing import Optional, Dict, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    JSON,
    UniqueConstraint,
    CheckConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

class UserDevice(Base):
    """
    UserDevice — tracks a user's devices and login sessions (mobile-first).

    Upgrades:
    - SQLAlchemy 2.0 typed mappings
    - Timezone-aware timestamps (first_seen_at / last_active_at)
    - Trust & revoke flags (account safety)
    - Optional push token for notifications
    - Fingerprint + UA hash for deduping / analytics
    - Helpful update helpers (touch, trust, revoke, set_push_token)
    """
    __tablename__ = "user_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Device identity & context
    device_type: Mapped[Optional[str]] = mapped_column(String(64), default=None, doc="Android | iPhone | Web | Desktop")
    device_info: Mapped[Optional[str]] = mapped_column(String(255), default=None, doc="e.g., Chrome on Windows 11")
    device_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), default=None, index=True, doc="Stable client-side fingerprint if available"
    )
    ua_hash: Mapped[Optional[str]] = mapped_column(
        String(64), default=None, index=True, doc="SHA-256 of user-agent for compact analytics"
    )

    # Network/location (optional)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    city: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    country: Mapped[Optional[str]] = mapped_column(String(64), default=None)

    # App/OS versions (optional)
    os_version: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    app_version: Mapped[Optional[str]] = mapped_column(String(32), default=None)

    # Notifications
    push_token: Mapped[Optional[str]] = mapped_column(String(255), default=None, index=True)

    # Security status
    is_trusted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Timestamps
    first_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    last_active_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, index=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Extra metadata (headers snapshot, device caps, etc.)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)

    # Relationship
    user: Mapped["User"] = relationship("User", back_populates="user_devices", lazy="selectin")

    __table_args__ = (
        # Optional de-dup if you consider (user_id, device_fingerprint) as unique
        UniqueConstraint("user_id", "device_fingerprint", name="uq_user_device_fingerprint"),
        Index("ix_user_devices_user_trusted", "user_id", "is_trusted"),
        Index("ix_user_devices_user_active", "user_id", "last_active_at"),
        CheckConstraint(
            "(push_token IS NULL) OR (length(push_token) >= 10)",
            name="ck_user_devices_push_token_len"
        ),
    )

    # ----------------- Helpers (no DB I/O here) -----------------

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def touch(
        self,
        *,
        ip: Optional[str] = None,
        ua: Optional[str] = None,
        city: Optional[str] = None,
        country: Optional[str] = None,
        app_version: Optional[str] = None,
        os_version: Optional[str] = None,
    ) -> None:
        """Update last_active_at and optionally refresh network/UA info."""
        self.last_active_at = dt.datetime.now(dt.timezone.utc)
        if ip:
            self.ip_address = ip[:64]
        if ua:
            self.ua_hash = self._sha256(ua)[:64]
        if city:
            self.city = city[:64]
        if country:
            self.country = country[:64]
        if app_version:
            self.app_version = app_version[:32]
        if os_version:
            self.os_version = os_version[:32]

    def set_push_token(self, token: Optional[str]) -> None:
        """Attach/replace the device push token for notifications."""
        self.push_token = (token or None)
        # Token normalization/validation can be added here per provider.

    def trust(self) -> None:
        """Mark device as trusted (e.g., after OTP)."""
        self.is_trusted = True
        self.is_revoked = False

    def revoke(self) -> None:
        """Revoke device (sign out & block until re-verified)."""
        self.is_revoked = True
        self.is_trusted = False

    def set_fingerprint(self, fp: Optional[str]) -> None:
        """Attach a stable device fingerprint if the client provides one."""
        self.device_fingerprint = (fp or None)[:64] if fp else None

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<UserDevice id={self.id} user={self.user_id} type={self.device_type or '-'} "
            f"trusted={self.is_trusted} revoked={self.is_revoked} last={self.last_active_at}>"
        )



