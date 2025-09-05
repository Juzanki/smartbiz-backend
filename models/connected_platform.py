# backend/models/connected_platform.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, TYPE_CHECKING, Dict, Any, Iterable

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
from sqlalchemy.orm import Mapped, mapped_column, relationship, backref, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for
from sqlalchemy.ext.mutable import MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

if TYPE_CHECKING:
    from .user import User


# --------- Enums ---------
class PlatformName(str, enum.Enum):
    telegram   = "telegram"
    whatsapp   = "whatsapp"
    sms        = "sms"
    facebook   = "facebook"
    instagram  = "instagram"
    tiktok     = "tiktok"
    twitter    = "twitter"
    youtube    = "youtube"
    email      = "email"
    other      = "other"

class ConnectStatus(str, enum.Enum):
    active  = "active"
    paused  = "paused"
    revoked = "revoked"
    error   = "error"

class AuthType(str, enum.Enum):
    oauth2  = "oauth2"
    apikey  = "apikey"
    session = "session"
    webhook = "webhook"
    none    = "none"


class ConnectedPlatform(Base):
    """Third-party platform linked to a user (tokens, webhooks, scopes & telemetry)."""
    __tablename__ = "connected_platforms"
    __mapper_args__ = {"eager_defaults": True}

    __table_args__ = (
        UniqueConstraint("user_id", "platform", "external_user_id", name="uq_platform_user_ext"),
        Index("ix_cp_user_platform", "user_id", "platform"),
        Index("ix_cp_status_updated", "status", "updated_at"),
        # SQLite + PG portable checks
        CheckConstraint("length(trim(preferred_language)) BETWEEN 2 AND 10", name="ck_cp_lang_len"),
        CheckConstraint("auth_type <> 'oauth2' OR access_token IS NOT NULL", name="ck_cp_oauth_has_token"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Owner
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Identity
    platform: Mapped[PlatformName] = mapped_column(
        SQLEnum(PlatformName, name="connected_platform_name", native_enum=False, validate_strings=True),
        nullable=False,
        index=True,
    )
    display_name: Mapped[Optional[str]] = mapped_column(String(120))
    handle: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    external_user_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    preferred_language: Mapped[str] = mapped_column(String(10), default="en", nullable=False)

    # Auth
    auth_type: Mapped[AuthType] = mapped_column(
        SQLEnum(AuthType, name="connected_platform_auth_type", native_enum=False, validate_strings=True),
        default=AuthType.apikey,
        nullable=False,
        index=True,
    )
    access_token: Mapped[Optional[str]] = mapped_column(String(1024))
    refresh_token: Mapped[Optional[str]] = mapped_column(String(1024))
    token_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))

    # Webhook / inbound
    webhook_url: Mapped[Optional[str]] = mapped_column(String(512))
    webhook_secret: Mapped[Optional[str]] = mapped_column(String(256))
    webhook_meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))

    # Status / verification
    status: Mapped[ConnectStatus] = mapped_column(
        SQLEnum(ConnectStatus, name="connected_platform_status", native_enum=False, validate_strings=True),
        default=ConnectStatus.active,
        nullable=False,
        index=True,
    )
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64))
    last_error_message: Mapped[Optional[str]] = mapped_column(String(400))

    # Telemetry
    connected_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    last_sync_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Audit
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))

    # Relationship
    user: Mapped["User"] = relationship(
        "User",
        backref=backref(
            "connected_platforms",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        foreign_keys=[user_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def is_token_expired(self) -> bool:
        return bool(self.token_expires_at and dt.datetime.now(dt.timezone.utc) >= self.token_expires_at)

    @hybrid_property
    def is_active(self) -> bool:
        return self.status == ConnectStatus.active and not self.is_token_expired

    @hybrid_property
    def is_verified_and_active(self) -> bool:
        return self.verified and self.is_active

    # ---------- Validators ----------
    @validates("preferred_language")
    def _norm_lang(self, _k: str, v: str) -> str:
        t = (v or "en").strip().replace("_", "-")[:10]
        return t if len(t) >= 2 else "en"

    @validates("display_name", "handle", "external_user_id", "webhook_url", "webhook_secret",
               "last_error_code", "last_error_message", "access_token", "refresh_token")
    def _trim_strings(self, key: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        limits = {
            "display_name": 120, "handle": 120, "external_user_id": 160,
            "webhook_url": 512, "webhook_secret": 256,
            "last_error_code": 64, "last_error_message": 400,
            "access_token": 1024, "refresh_token": 1024,
        }
        return v[:limits.get(key, 255)] or None

    # ---------- Helpers (no DB commit hapa) ----------
    def mark_error(self, code: str | None, message: str | None = None) -> None:
        self.status = ConnectStatus.error
        self.last_error_code = (code or "")[:64] or None
        self.last_error_message = (message or "")[:400] or None

    def pause(self) -> None:
        self.status = ConnectStatus.paused

    def activate(self) -> None:
        self.status = ConnectStatus.active
        self.last_error_code = None
        self.last_error_message = None

    def revoke(self) -> None:
        """Hard-revoke tokens and disable usage."""
        self.status = ConnectStatus.revoked
        self.access_token = None
        self.refresh_token = None
        self.token_expires_at = None

    def rotate_access_token(self, new_token: str, *, ttl_seconds: int | None = None) -> None:
        self.access_token = (new_token or "").strip()[:1024] or None
        if ttl_seconds:
            self.token_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(ttl_seconds))

    def ensure_scopes(self, updates: Dict[str, Any]) -> None:
        """Merge/update `scopes` as a dict; useful kwa OAuth consent refresh."""
        data = dict(self.scopes or {})
        for k, v in (updates or {}).items():
            data[str(k)] = v
        self.scopes = data

    def has_scope(self, key: str, default: bool = False) -> bool:
        try:
            return bool((self.scopes or {}).get(key, default))
        except Exception:
            return default

    def set_webhook(self, *, url: Optional[str], secret: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> None:
        self.webhook_url = (url or "").strip()[:512] or None
        self.webhook_secret = (secret or "").strip()[:256] or None
        if meta is not None:
            self.webhook_meta = dict(meta)

    def touch_sync(self) -> None:
        self.last_sync_at = dt.datetime.now(dt.timezone.utc)

    def touch_event(self) -> None:
        self.last_event_at = dt.datetime.now(dt.timezone.utc)

    def mask_token(self) -> str:
        tok = self.access_token or ""
        return f"{tok[:4]}...{tok[-4:]}" if len(tok) >= 8 else ("*" * len(tok))

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ConnectedPlatform id={self.id} user={self.user_id} "
            f"platform={self.platform} status={self.status} verified={self.verified}>"
        )


# ---------- Normalizers (events) ----------
@listens_for(ConnectedPlatform, "before_insert")
def _cp_before_insert(_m, _c, t: ConnectedPlatform) -> None:
    # Force lowercase handles; trim lengths
    if t.handle:
        t.handle = t.handle.strip()[:120]
        if t.platform in {PlatformName.twitter, PlatformName.instagram, PlatformName.tiktok}:
            t.handle = t.handle.lstrip("@")
    if t.display_name:
        t.display_name = t.display_name.strip()[:120]
    if t.external_user_id:
        t.external_user_id = t.external_user_id.strip()[:160]
    if t.webhook_url:
        t.webhook_url = t.webhook_url.strip()[:512]
    if t.webhook_secret:
        t.webhook_secret = t.webhook_secret.strip()[:256]
    if t.last_error_code:
        t.last_error_code = t.last_error_code.strip()[:64]
    if t.last_error_message:
        t.last_error_message = t.last_error_message.strip()[:400]
    if t.access_token:
        t.access_token = t.access_token.strip()[:1024]
    if t.refresh_token:
        t.refresh_token = t.refresh_token.strip()[:1024]


@listens_for(ConnectedPlatform, "before_update")
def _cp_before_update(_m, _c, t: ConnectedPlatform) -> None:
    _cp_before_insert(_m, _c, t)  # normalize the same way
