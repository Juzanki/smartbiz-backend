# backend/models/api_key.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import secrets
import hashlib
import datetime as dt
from typing import Optional, TYPE_CHECKING, List, Dict, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    CheckConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for
from sqlalchemy.ext.mutable import MutableList, MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

if TYPE_CHECKING:
    from .user import User


class APIKey(Base):
    """
    API keys for system integrations & user access.

    Security model:
      - Store only SHA-256 hash of the key (hex, 64 chars)
      - Keep small 'key_prefix' (first 6 chars) + 'last_four' for UX / quick lookup
      - Raw key is returned only at creation/rotation time by service layer

    Extras:
      - Optional scopes (list[str])
      - Optional IP allowlist (list[str]/CIDRs)
      - Rate limits (per minute / hour)
      - Usage counters & timestamps
    """
    __tablename__ = "api_keys"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Human-friendly name
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)

    # Key identity (DO NOT store raw key):
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)  # sha256 hex
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False, index=True)  # first 6..12 chars
    last_four: Mapped[str] = mapped_column(String(4), nullable=False)

    # Optional description
    description: Mapped[Optional[str]] = mapped_column(Text)

    # Owner
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    user: Mapped[Optional["User"]] = relationship("User", backref="api_keys", lazy="selectin")

    # Status & lifecycle
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Limits
    rate_limit_per_minute: Mapped[Optional[int]] = mapped_column(Integer)
    rate_limit_per_hour:   Mapped[Optional[int]] = mapped_column(Integer)

    # Policy / metadata
    scopes: Mapped[Optional[List[str]]] = mapped_column(MutableList.as_mutable(JSON_VARIANT))
    ip_allowlist: Mapped[Optional[List[str]]] = mapped_column(MutableList.as_mutable(JSON_VARIANT))
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))

    # -------------------- Hybrid properties --------------------
    @hybrid_property
    def is_expired(self) -> bool:
        return bool(self.expires_at and dt.datetime.now(dt.timezone.utc) >= self.expires_at)

    @hybrid_property
    def is_active(self) -> bool:
        return (not self.revoked) and (not self.is_expired)

    # -------------------- Helpers / Domain logic --------------------
    @staticmethod
    def generate_key(length: int = 40) -> str:
        """
        Generate a cryptographically secure key (URL-safe).
        Returns raw key string (caller should show once).
        """
        return secrets.token_urlsafe(length)[:length]

    @staticmethod
    def _sha256_hex(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _prefix_of(value: str, size: int = 6) -> str:
        size = max(4, min(size, 12))
        return value[:size]

    def set_raw_key(self, raw_key: str, *, prefix_len: int = 6) -> None:
        """
        Set credentials from a raw key. Stores only hash/prefix/last4.
        """
        rk = (raw_key or "").strip()
        if len(rk) < 16:
            raise ValueError("API key must be at least 16 characters.")
        self.key_hash = self._sha256_hex(rk)
        self.key_prefix = self._prefix_of(rk, prefix_len)
        self.last_four = rk[-4:]

    def verify(self, raw_key: str) -> bool:
        """Constant-time-ish verification by comparing sha256 hex."""
        return self.key_hash == self._sha256_hex(raw_key or "")

    def rotate(self, *, length: int = 40, prefix_len: int = 6) -> str:
        """
        Rotate to a new random key.
        Returns the NEW raw key (remember to show it once to the user).
        """
        new_raw = self.generate_key(length)
        self.set_raw_key(new_raw, prefix_len=prefix_len)
        return new_raw

    def revoke(self) -> None:
        self.revoked = True

    def reactivate(self) -> None:
        if self.is_expired:
            raise ValueError("Cannot reactivate an expired key.")
        self.revoked = False

    def mark_used(self) -> None:
        self.last_used_at = dt.datetime.now(dt.timezone.utc)
        self.usage_count = (self.usage_count or 0) + 1

    def __repr__(self) -> str:  # pragma: no cover
        preview = f"{self.key_prefix}...{self.last_four}" if self.key_prefix and self.last_four else "N/A"
        return f"<APIKey id={self.id} name={self.name!r} active={self.is_active} key={preview}>"

    # -------------------- Table Constraints --------------------
    __table_args__ = (
        CheckConstraint("length(trim(name)) >= 3", name="ck_api_key_name_min_len"),
        CheckConstraint("length(key_hash) = 64", name="ck_api_key_hash_len64"),  # sha256 hex
        CheckConstraint("length(last_four) = 4", name="ck_api_key_last4_len4"),
        CheckConstraint(
            "rate_limit_per_minute IS NULL OR rate_limit_per_minute >= 0",
            name="ck_api_key_rl_min_nonneg",
        ),
        CheckConstraint(
            "rate_limit_per_hour IS NULL OR rate_limit_per_hour >= 0",
            name="ck_api_key_rl_hour_nonneg",
        ),
    )


# Helpful composite indexes
Index("ix_api_keys_user_active", APIKey.user_id, APIKey.revoked, APIKey.expires_at)
Index("ix_api_keys_prefix", APIKey.key_prefix)

# -------------------- Normalizers / Guards --------------------
@listens_for(APIKey, "before_insert")
def _api_key_before_insert(_m, _c, k: APIKey) -> None:
    if k.name:
        k.name = k.name.strip()[:80]
    if k.description:
        k.description = k.description.strip()
    # minimal sanity on lists
    if k.scopes:
        k.scopes = sorted({(s or "").strip().lower() for s in k.scopes if s})
    if k.ip_allowlist:
        # Keep as-is but trimmed; CIDR validation can live in service layer
        k.ip_allowlist = [(ip or "").strip() for ip in k.ip_allowlist if ip]


@listens_for(APIKey, "before_update")
def _api_key_before_update(_m, _c, k: APIKey) -> None:
    if k.name:
        k.name = k.name.strip()[:80]
    if k.description:
        k.description = k.description.strip()
    if k.scopes:
        k.scopes = sorted({(s or "").strip().lower() for s in k.scopes if s})
    if k.ip_allowlist:
        k.ip_allowlist = [(ip or "").strip() for ip in k.ip_allowlist if ip]
