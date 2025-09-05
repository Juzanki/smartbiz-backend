# backend/models/push_subscription.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import enum
import hashlib
from typing import Optional, TYPE_CHECKING, Dict, Any, List

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
    text,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# ---- Portable JSON: JSON_VARIANT on Postgres; JSON elsewhere ----
try:
    pass
except Exception:  # pragma: no cover
    # patched: use shared JSON_VARIANT
    ...

if TYPE_CHECKING:
    from .user import User


class PushEncoding(str, enum.Enum):
    aesgcm = "aesgcm"        # Chrome/Firefox/Edge
    aes128gcm = "aes128gcm"  # Safari
    other = "other"


class PushPlatform(str, enum.Enum):
    web = "web"
    android = "android"
    ios = "ios"
    desktop = "desktop"
    other = "other"


class PushPriority(str, enum.Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class PushSubscription(Base):
    """
    Web/Native Push subscription:
      - Core VAPID keys (endpoint/p256dh/auth + encoding)
      - Context: platform/origin/app/device
      - Delivery policy: priority, ttl_seconds, collapse_key
      - Governance: quotas, throttling, retry/backoff, DND window
      - Targeting: topics/tags + locale
      - Health/analytics: failure counts, last_success/error, next_attempt
    """
    __tablename__ = "push_subscriptions"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        # Uniqueness / indexing
        UniqueConstraint("endpoint", name="uq_push_endpoint"),
        UniqueConstraint("endpoint_hash", name="uq_push_endpoint_hash"),
        Index("ix_push_user_created", "user_id", "created_at"),
        Index("ix_push_valid_revoked", "is_valid", "is_revoked"),
        Index("ix_push_last_push", "last_push_at"),
        Index("ix_push_origin", "origin"),
        Index("ix_push_platform", "platform"),
        Index("ix_push_topics", "user_id", "platform", "is_valid"),
        Index("ix_push_next_attempt", "next_attempt_at"),
        Index("ix_push_throttle", "throttle_until"),
        # Guards
        CheckConstraint("length(endpoint) >= 12", name="ck_push_endpoint_len"),
        CheckConstraint("length(p256dh) >= 10", name="ck_push_p256dh_len"),
        CheckConstraint("length(auth) >= 6", name="ck_push_auth_len"),
        CheckConstraint("(ttl_seconds IS NULL) OR (ttl_seconds BETWEEN 0 AND 2419200)",  # max ~28 days (fcm)
                        name="ck_push_ttl_range"),
        CheckConstraint("(dnd_start_minute IS NULL) OR (dnd_start_minute BETWEEN 0 AND 1439)",
                        name="ck_push_dnd_start_range"),
        CheckConstraint("(dnd_end_minute IS NULL) OR (dnd_end_minute BETWEEN 0 AND 1439)",
                        name="ck_push_dnd_end_range"),
        CheckConstraint("daily_quota >= 0 AND sent_count_24h >= 0", name="ck_push_quota_nonneg"),
        CheckConstraint("(max_retries IS NULL) OR (max_retries >= 0)", name="ck_push_max_retries_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Owner
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user: Mapped["User"] = relationship(
        "User",
        back_populates="push_subscriptions",
        passive_deletes=True,
        lazy="selectin",
    )

    # Core subscription
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)   # typically https://...
    endpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    p256dh: Mapped[str] = mapped_column(String(256), nullable=False)     # base64url
    auth: Mapped[str] = mapped_column(String(128), nullable=False)       # base64url (auth secret)
    encoding: Mapped[PushEncoding] = mapped_column(
        SQLEnum(PushEncoding, name="push_encoding"),
        default=PushEncoding.aesgcm,
        nullable=False,
        index=True,
    )

    # Context / metadata
    platform: Mapped[PushPlatform] = mapped_column(
        SQLEnum(PushPlatform, name="push_platform"),
        default=PushPlatform.web,
        nullable=False,
        index=True,
    )
    origin: Mapped[Optional[str]] = mapped_column(String(255), index=True)     # e.g. https://app.example.com
    app_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)      # bundle/app identifier
    app_version: Mapped[Optional[str]] = mapped_column(String(32))
    user_agent: Mapped[Optional[str]] = mapped_column(String(400))
    device_id: Mapped[Optional[str]] = mapped_column(String(120), index=True)  # client-generated ID
    device_model: Mapped[Optional[str]] = mapped_column(String(120))
    device_type: Mapped[Optional[str]] = mapped_column(String(80))             # e.g. "Chrome-Android"
    os_name: Mapped[Optional[str]] = mapped_column(String(40))
    os_version: Mapped[Optional[str]] = mapped_column(String(32))
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    locale: Mapped[Optional[str]] = mapped_column(String(12), index=True)      # e.g., "sw-TZ"
    timezone: Mapped[Optional[str]] = mapped_column(String(64), default="UTC")

    # Targeting
    topics: Mapped[Optional[List[str]]] = mapped_column(as_mutable_json(JSON_VARIANT))   # ["orders","promos"]
    tags:   Mapped[Optional[List[str]]] = mapped_column(as_mutable_json(JSON_VARIANT))   # ["vip","beta"]

    # Delivery policy
    priority: Mapped[PushPriority] = mapped_column(
        SQLEnum(PushPriority, name="push_priority"),
        default=PushPriority.normal,
        nullable=False,
        index=True,
    )
    ttl_seconds: Mapped[Optional[int]] = mapped_column(Integer)             # time-to-live hint
    collapse_key: Mapped[Optional[str]] = mapped_column(String(64))         # dedupe on device
    data_defaults: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Validity & lifecycle
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    expiration_time: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # DND window
    dnd_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    dnd_start_minute: Mapped[Optional[int]] = mapped_column(Integer)  # 0..1439
    dnd_end_minute:   Mapped[Optional[int]] = mapped_column(Integer)  # 0..1439

    # Quotas & throttling
    daily_quota: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("500"))
    sent_count_24h: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sent_reset_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    throttle_until: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Retry/backoff
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_retries: Mapped[Optional[int]] = mapped_column(Integer)  # None = no hard cap
    last_attempt_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Delivery health / audit
    last_error_code: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    last_error_msg:  Mapped[Optional[str]] = mapped_column(Text)
    failure_count:   Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_push_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    last_seen_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Free-form data
    meta: Mapped[Optional[dict]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Audit
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def active(self) -> bool:
        if not self.is_valid or self.is_revoked:
            return False
        if self.expiration_time and _utcnow() >= self.expiration_time:
            return False
        return True

    @hybrid_property
    def should_prune(self) -> bool:
        return (self.failure_count or 0) >= 10 or not self.is_valid or self.is_revoked

    # ---------- Validators ----------
    @validates("endpoint")
    def _v_endpoint(self, _k: str, v: str) -> str:
        v = (v or "").strip()
        # set endpoint_hash whenever endpoint changes
        self.endpoint_hash = hashlib.sha256(v.encode("utf-8")).hexdigest() if v else ""
        return v

    # ---------- DND helpers ----------
    def _local_minutes_now(self, when: Optional[dt.datetime] = None) -> int:
        when = when or _utcnow()
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self.timezone or "UTC")
            local = when.astimezone(tz)
        except Exception:
            local = when
        return local.hour * 60 + local.minute

    def is_dnd_active(self, when: Optional[dt.datetime] = None) -> bool:
        if not self.dnd_enabled:
            return False
        if self.dnd_start_minute is None or self.dnd_end_minute is None:
            return False
        now_min = self._local_minutes_now(when)
        start, end = int(self.dnd_start_minute), int(self.dnd_end_minute)
        if start == end:
            return False
        if start < end:
            return start <= now_min < end
        return now_min >= start or now_min < end  # over-midnight

    # ---------- Governance ----------
    def _reset_quota_if_needed(self) -> None:
        """Reset rolling 24h counter if window elapsed."""
        if not self.sent_reset_at or (_utcnow() - self.sent_reset_at).total_seconds() >= 24 * 3600:
            self.sent_count_24h = 0
            self.sent_reset_at = _utcnow()

    def can_send(self, *, priority: PushPriority | str = PushPriority.normal,
                 when: Optional[dt.datetime] = None) -> bool:
        """
        Kagua kama tunaweza kutuma sasa:
          - subscription active
          - si DND (isipokuwa priority high/urgent)
          - quota haijavuka
          - si throttled / before next_attempt_at
        """
        prio = PushPriority(str(priority))
        if not self.active:
            return False

        self._reset_quota_if_needed()
        if self.throttle_until and (_utcnow() < self.throttle_until):
            # allow only urgent to bypass throttle
            if prio not in (PushPriority.high, PushPriority.urgent):
                return False

        if self.next_attempt_at and (_utcnow() < self.next_attempt_at):
            if prio not in (PushPriority.high, PushPriority.urgent):
                return False

        if self.is_dnd_active(when) and prio not in (PushPriority.high, PushPriority.urgent):
            return False

        if self.sent_count_24h >= self.daily_quota and prio not in (PushPriority.high, PushPriority.urgent):
            return False

        return True

    # ---------- Delivery updates ----------
    def touch_seen(self) -> None:
        self.last_seen_at = _utcnow()

    def mark_success(self) -> None:
        now = _utcnow()
        self.last_push_at = now
        self.last_success_at = now
        self.last_error_code = None
        self.last_error_msg = None
        self.failure_count = 0
        self.attempt_count = 0
        self.next_attempt_at = None
        self.is_valid = True
        self._reset_quota_if_needed()
        self.sent_count_24h = (self.sent_count_24h or 0) + 1

    def mark_temp_failure(self, *, code: Optional[str] = None, message: Optional[str] = None,
                          base_backoff: int = 60, factor: float = 1.6) -> None:
        """
        Rekodi kosa la muda + panga jaribio jingine (exponential backoff iliyozuiliwa).
        """
        now = _utcnow()
        self.last_attempt_at = now
        self.last_error_code = code
        self.last_error_msg = message
        self.attempt_count = (self.attempt_count or 0) + 1

        # 404/410 (gone) => invalidate
        if (code or "").lower() in {"404", "410", "gone", "not_found"}:
            self.is_valid = False
            self.next_attempt_at = None
            return

        # compute next attempt
        delay = int(base_backoff * (factor ** max(0, (self.attempt_count - 1))))
        delay = max(10, min(delay, 3600))  # clamp 10s..60m
        self.next_attempt_at = now + dt.timedelta(seconds=delay)

    def mark_failure(self, *, code: Optional[str] = None, message: Optional[str] = None) -> None:
        self.failure_count = (self.failure_count or 0) + 1
        self.last_error_code = code
        self.last_error_msg = message
        # terminal invalidation for gone endpoints
        if (code or "").lower() in {"404", "410", "gone", "not_found"}:
            self.is_valid = False
        # optional: hard cap on retries
        if self.max_retries is not None and (self.attempt_count or 0) >= self.max_retries:
            self.next_attempt_at = None

    def revoke(self, *, reason: Optional[str] = None) -> None:
        self.is_revoked = True
        self.is_valid = False
        if reason:
            self.meta = {**(self.meta or {}), "revoked_reason": reason}

    def rotate_keys(self, *, p256dh: str, auth: str, encoding: Optional[PushEncoding] = None) -> None:
        """Sasisha funguo (subscription mpya kutoka kwa mteja)."""
        self.p256dh = p256dh
        self.auth = auth
        if encoding:
            self.encoding = encoding
        self.is_valid = True
        self.is_revoked = False
        self.failure_count = 0
        self.attempt_count = 0
        self.last_error_code = None
        self.last_error_msg = None
        self.next_attempt_at = None

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<PushSubscription id={self.id} user={self.user_id} active={self.active} "
                f"platform={self.platform} prio={self.priority}>")
