# backend/models/webhook.py
# -*- coding: utf-8 -*-
"""
WebhookEndpoint — stores user's webhook URLs, secrets, and settings.
Paired with WebhookDeliveryLog in backend/models/webhook_delivery_log.py
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from hmac import new as hmac_new
from hashlib import sha256
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    JSON,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

class WebhookEndpoint(Base):
    """Webhook endpoint definition per user."""

    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        UniqueConstraint("user_id", "url", name="uq_webhook_user_url"),
        Index("ix_webhook_user_active", "user_id", "is_active"),
        Index("ix_webhook_created_at", "created_at"),
        CheckConstraint("length(url) > 0", name="ck_webhook_url_nonempty"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Destination URL (HTTPS strongly recommended)
    url: Mapped[str] = mapped_column(String(255), nullable=False)

    # Secret for signing payloads
    secret: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    description: Mapped[Optional[str]] = mapped_column(String(255), default=None)

    # Event filtering — if null/empty, receive all events
    subscribed_events: Mapped[Optional[List[str]]] = mapped_column(JSON, default=None)

    # Delivery tuning
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    rate_limit_per_minute: Mapped[Optional[int]] = mapped_column(Integer, default=None)

    # Timestamps (TZ-aware)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # ---------------- Relationships ----------------
    # IMPORTANT: tumia jina la relationship HALISI upande wa User: "webhook_endpoints"
    user: Mapped["User"] = relationship("User", back_populates="webhook_endpoints", lazy="selectin")
    deliveries: Mapped[list["WebhookDeliveryLog"]] = relationship(
        "WebhookDeliveryLog",
        back_populates="endpoint",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # ---------------- Utilities ----------------
    def sign_payload(self, payload: Union[str, bytes]) -> str:
        """HMAC-SHA256 of payload using `secret`. Returns hex digest or empty string if no secret."""
        if not self.secret:
            return ""
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        return hmac_new(self.secret.encode("utf-8"), data, sha256).hexdigest()

    def verify_signature(self, payload: Union[str, bytes], signature: str) -> bool:
        """Constant-time compare; returns False if no secret set."""
        if not self.secret:
            return False
        expected = self.sign_payload(payload)
        return secrets.compare_digest(expected, signature)

    def rotate_secret(self, length: int = 32) -> str:
        """Generate and store a new random secret."""
        self.secret = secrets.token_hex(length)
        self.updated_at = datetime.now(timezone.utc)
        return self.secret

    def to_public_dict(self) -> Dict[str, Any]:
        """Safe API response (no secret)."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "url": self.url,
            "description": self.description,
            "subscribed_events": self.subscribed_events or [],
            "is_active": self.is_active,
            "max_retries": self.max_retries,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover
        status = "active" if self.is_active else "inactive"
        return f"<WebhookEndpoint #{self.id} url={self.url} user={self.user_id} ({status})>"




