# backend/models/platform_status.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import enum
from typing import Optional, Dict, Any

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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------- Enums ---------
class PlatformKind(str, enum.Enum):
    whatsapp   = "whatsapp"
    telegram   = "telegram"
    facebook   = "facebook"
    instagram  = "instagram"
    sms        = "sms"
    email      = "email"
    tiktok     = "tiktok"
    youtube    = "youtube"
    other      = "other"


class ConnectionState(str, enum.Enum):
    disconnected = "disconnected"
    connecting   = "connecting"
    connected    = "connected"
    error        = "error"
    expired      = "expired"


class Health(str, enum.Enum):
    green  = "green"
    yellow = "yellow"
    red    = "red"


class TokenKind(str, enum.Enum):
    oauth      = "oauth"
    long_lived = "long_lived"
    api_key    = "api_key"
    session    = "session"
    other      = "other"


class WebhookMode(str, enum.Enum):
    disabled = "disabled"
    passive  = "passive"   # log only
    active   = "active"    # deliver


# --------- Model ---------
class PlatformStatus(Base):
    """
    Hali ya uunganisho wa jukwaa (per-user, per-platform).
    - State/health + SLA freshness (kutoka last_check_at)
    - Token metadata (kind/scopes/rotations/expiry)
    - Webhook health (fail counts/backoff/last delivery)
    - Reconnect backoff ya kiotomatiki + helpers
    - JSON meta/scopes ni mutable (tracked)
    """
    __tablename__ = "platform_statuses"
    __mapper_args__ = {"eager_defaults": True}

    __table_args__ = (
        UniqueConstraint("user_id", "platform", name="uq_platform_status_user_platform"),
        Index("ix_plat_status_user_created", "user_id", "created_at"),
        Index("ix_plat_status_state_time", "state", "updated_at"),
        Index("ix_plat_status_token_expiry", "access_token_expiry"),
        Index("ix_plat_status_user_platform_state", "user_id", "platform", "state"),
        Index("ix_plat_status_backoff", "backoff_until"),
        Index("ix_plat_status_health", "health"),
        CheckConstraint("length(status_note) <= 255", name="ck_plat_status_note_len"),
        CheckConstraint("reconnect_attempts >= 0", name="ck_plat_status_retries_nonneg"),
        CheckConstraint("webhook_fail_count >= 0", name="ck_plat_webhook_fail_nonneg"),
        {"extend_existing": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Owner
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # linga na User.platform_statuses
    user: Mapped["User"] = relationship(
        "User",
        back_populates="platform_statuses",
        foreign_keys=lambda: [PlatformStatus.user_id],
        passive_deletes=True,
        lazy="selectin",
    )

    platform: Mapped[PlatformKind] = mapped_column(
        SQLEnum(PlatformKind, name="platform_kind", native_enum=False, validate_strings=True),
        nullable=False,
        index=True,
    )

    # State & health
    state: Mapped[ConnectionState] = mapped_column(
        SQLEnum(ConnectionState, name="platform_state", native_enum=False, validate_strings=True),
        default=ConnectionState.disconnected,
        nullable=False,
        index=True,
    )
    health: Mapped[Health] = mapped_column(
        SQLEnum(Health, name="platform_health", native_enum=False, validate_strings=True),
        default=Health.red,
        nullable=False,
        index=True,
    )

    is_connected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    last_connected_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    last_check_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Token metadata
    token_kind: Mapped[TokenKind] = mapped_column(
        SQLEnum(TokenKind, name="platform_token_kind", native_enum=False, validate_strings=True),
        default=TokenKind.oauth,
        nullable=False,
        index=True,
    )
    access_token_expiry: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    refresh_after: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    last_token_rotated_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[Optional[Dict[str, bool]]] = mapped_column(as_mutable_json(JSON_VARIANT))  # {"messages":true,"media":false}
    token_meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Diagnostics / control
    status_note: Mapped[Optional[str]] = mapped_column(String(255))
    error_code: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text)
    reconnect_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Backoff (exponential) for reconnects / webhooks
    backoff_until: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    backoff_base_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    backoff_max_seconds:  Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3600"))

    # Webhook delivery health
    webhook_mode: Mapped[WebhookMode] = mapped_column(
        SQLEnum(WebhookMode, name="platform_webhook_mode", native_enum=False, validate_strings=True),
        default=WebhookMode.active, nullable=False, index=True,
    )
    webhook_url: Mapped[Optional[str]] = mapped_column(String(512))
    last_webhook_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    webhook_fail_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Free-form metadata (permissions, channel ids, etc.)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Audit
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ---------- Hybrid properties ----------
    @hybrid_property
    def token_expired(self) -> bool:
        return bool(self.access_token_expiry and _utcnow() >= self.access_token_expiry)

    @hybrid_property
    def needs_reauth(self) -> bool:
        return self.token_expired or self.state in (ConnectionState.expired, ConnectionState.error)

    @hybrid_property
    def minutes_since_check(self) -> int:
        if not self.last_check_at:
            return 10**9
        return max(0, int((_utcnow() - self.last_check_at).total_seconds() // 60))

    @hybrid_property
    def seconds_to_expiry(self) -> int:
        if not self.access_token_expiry:
            return 0
        return max(0, int((self.access_token_expiry - _utcnow()).total_seconds()))

    @hybrid_property
    def sla_unhealthy(self) -> bool:
        """True ikiwa health inaleta wasiwasi kwa SLA ya “freshness” (>15 min bila check au token karibu ku-expire)."""
        stale = self.minutes_since_check > 15
        near_expiry = self.seconds_to_expiry > 0 and self.seconds_to_expiry < 900  # <15min
        return stale or near_expiry or self.health is Health.red

    @hybrid_property
    def can_attempt_reconnect(self) -> bool:
        return (self.backoff_until is None) or (_utcnow() >= self.backoff_until)

    # ---------- State helpers ----------
    def bump_check(self) -> None:
        self.last_check_at = _utcnow()

    def mark_connected(
        self, *, token_expiry: Optional[dt.datetime] = None, note: Optional[str] = None, scopes: Optional[Dict[str, bool]] = None
    ) -> None:
        self.state = ConnectionState.connected
        self.health = Health.green
        self.is_connected = True
        self.last_connected_at = _utcnow()
        if token_expiry:
            self.access_token_expiry = token_expiry
        if scopes is not None:
            self.scopes = scopes
        if note:
            self.status_note = note
        # clear errors/backoff
        self.error_code = None
        self.error_detail = None
        self.reconnect_attempts = 0
        self.backoff_until = None
        self.bump_check()

    def mark_disconnected(self, *, note: Optional[str] = None) -> None:
        self.state = ConnectionState.disconnected
        self.health = Health.yellow
        self.is_connected = False
        if note:
            self.status_note = note
        self.bump_check()

    def mark_error(
        self, *, code: Optional[str] = None, detail: Optional[str] = None, note: Optional[str] = None
    ) -> None:
        self.state = ConnectionState.error
        self.health = Health.red
        self.is_connected = False
        self.error_code = code
        self.error_detail = detail
        if note:
            self.status_note = note
        self.reconnect_attempts = (self.reconnect_attempts or 0) + 1
        self.schedule_backoff()
        self.bump_check()

    def mark_expired(self, *, note: Optional[str] = None) -> None:
        self.state = ConnectionState.expired
        self.health = Health.red
        self.is_connected = False
        if note:
            self.status_note = note
        self.schedule_backoff()
        self.bump_check()

    def token_rotated(self, *, new_expiry: Optional[dt.datetime] = None) -> None:
        self.last_token_rotated_at = _utcnow()
        if new_expiry:
            self.access_token_expiry = new_expiry
        # baada ya rotation, jaribu kuwasiliana tena bila backoff
        self.backoff_until = None

    # ---------- Backoff & webhook helpers ----------
    def schedule_backoff(self) -> None:
        """Exponential backoff (bounded) kulingana na idadi ya majaribio."""
        base = int(self.backoff_base_seconds or 5)
        max_s = int(self.backoff_max_seconds or 3600)
        n = max(1, int(self.reconnect_attempts or 1))
        delay = min(max_s, base * (2 ** (n - 1)))
        self.backoff_until = _utcnow() + dt.timedelta(seconds=delay)

    def webhook_delivered(self) -> None:
        self.last_webhook_at = _utcnow()
        self.webhook_fail_count = 0

    def webhook_failed(self) -> None:
        self.webhook_fail_count = (self.webhook_fail_count or 0) + 1
        # On repeated failures, degrade health and backoff
        if self.webhook_fail_count >= 3:
            self.health = Health.yellow
        if self.webhook_fail_count >= 5:
            self.health = Health.red
            self.schedule_backoff()

    # ---------- Validators ----------
    @validates("status_note", "error_code", "webhook_url")
    def _trim_text(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    # ---------- Repr ----------
    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<PlatformStatus user={self.user_id} platform={self.platform} "
            f"state={self.state} health={self.health} connected={self.is_connected}>"
        )


# ---------- Hooks ----------
@listens_for(PlatformStatus, "before_insert")
def _ps_before_insert(_m, _c, t: PlatformStatus) -> None:  # pragma: no cover
    # normalize url & clamp backoff bounds
    if t.webhook_url:
        t.webhook_url = t.webhook_url.strip()
    if t.backoff_base_seconds and t.backoff_base_seconds < 1:
        t.backoff_base_seconds = 1
    if t.backoff_max_seconds and t.backoff_max_seconds < t.backoff_base_seconds:
        t.backoff_max_seconds = t.backoff_base_seconds
    t.bump_check()


@listens_for(PlatformStatus, "before_update")
def _ps_before_update(_m, _c, t: PlatformStatus) -> None:  # pragma: no cover
    if t.webhook_url:
        t.webhook_url = t.webhook_url.strip()
    # keep bounds sane
    if t.backoff_base_seconds and t.backoff_base_seconds < 1:
        t.backoff_base_seconds = 1
    if t.backoff_max_seconds and t.backoff_max_seconds < t.backoff_base_seconds:
        t.backoff_max_seconds = t.backoff_base_seconds
