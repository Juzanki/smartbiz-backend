# backend/models/user_bot.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import uuid
import datetime as dt
from typing import Optional, List, Dict, Any, Iterable

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# ------------- Helpers -------------
def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _list_default() -> list[str]:
    # default empty allow-list; client inaweza ku-enable baadaye
    return []

def _dict_default() -> dict[str, Any]:
    # per-bot knobs used by mobile/web clients
    return {"greeting": "Hello! I’m your SmartBiz bot.", "tone": "friendly", "temperature": 0.6}

def _slugify(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = " ".join(s.strip().split()).lower()
    # make it URL-ish: spaces -> '-', trim length
    return s.replace(" ", "-")[:64] or None


class UserBot(Base):
    """
    UserBot — an AI bot instance owned by a user and tied to a subscription package.

    Features:
      • Typed SQLAlchemy 2.0 mappings + eager_defaults
      • Portable JSON (Postgres JSONB, others JSON) with mutable tracking
      • Strong constraints & indices
      • API key rotation helper
      • Quotas & usage counters with guard methods (can_send / mark_sent)
      • Soft-delete & suspension flow
    """
    __tablename__ = "user_bots"
    __mapper_args__ = {"eager_defaults": True}

    # --- Keys ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bot_package_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bot_packages.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # --- Identity ---
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(
        String(64), default=None, index=True, doc="URL/handle-friendly label"
    )
    purpose: Mapped[Optional[str]] = mapped_column(Text, default=None)

    # --- Lifecycle / status ---
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    suspended_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    suspend_reason: Mapped[Optional[str]] = mapped_column(String(255), default=None)

    # --- Integrations ---
    api_key: Mapped[Optional[str]] = mapped_column(String(64), default=None, unique=True, index=True)
    webhook_url: Mapped[Optional[str]] = mapped_column(String(255), default=None)

    platforms: Mapped[List[str]] = mapped_column(
        as_mutable_json(JSON_VARIANT), default=_list_default, nullable=False, doc='["telegram","whatsapp","sms"]'
    )
    config: Mapped[Dict[str, Any]] = mapped_column(
        as_mutable_json(JSON_VARIANT), default=_dict_default, nullable=False, doc="Model/settings per bot"
    )

    # --- Quotas & usage (simple counters; reset via daily/monthly cron) ---
    daily_quota: Mapped[int] = mapped_column(Integer, server_default=sa_text("1000"), nullable=False)
    monthly_quota: Mapped[int] = mapped_column(Integer, server_default=sa_text("20000"), nullable=False)
    messages_sent: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"), nullable=False)
    messages_sent_today: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"), nullable=False)
    messages_sent_month: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"), nullable=False)

    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)

    # --- Timestamps ---
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa_text("CURRENT_TIMESTAMP"),
        onupdate=sa_text("CURRENT_TIMESTAMP"), nullable=False
    )

    # --- Relationships ---
    # NOTE: foreign_keys=[user_id] huondoa AmbiguousForeignKeysError
    user: Mapped["User"] = relationship(
        "User", back_populates="bots", foreign_keys=[user_id], lazy="selectin", passive_deletes=True
    )
    package: Mapped["BotPackage"] = relationship("BotPackage", back_populates="bots", lazy="joined")

    # --- Table args ---
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_user_bots_user_name"),
        # aidha ruhusu slug unique kwa kila user
        UniqueConstraint("user_id", "slug", name="uq_user_bots_user_slug"),
        CheckConstraint("daily_quota >= 0 AND monthly_quota >= 0", name="ck_user_bots_quotas_nonneg"),
        CheckConstraint(
            "messages_sent >= 0 AND messages_sent_today >= 0 AND messages_sent_month >= 0",
            name="ck_user_bots_usage_nonneg",
        ),
        Index("ix_user_bots_user_active", "user_id", "is_active"),
        Index("ix_user_bots_pkg_active", "bot_package_id", "is_active"),
    )

    # ----------------- Helpers (no DB I/O here) -----------------
    def set_name(self, value: str) -> None:
        self.name = (value or "").strip()[:100]

    def set_slug(self, value: Optional[str]) -> None:
        self.slug = _slugify(value)

    def rotate_api_key(self) -> str:
        """Generate and set a new API key; returns the plaintext key for the caller to persist."""
        key = uuid.uuid4().hex + uuid.uuid4().hex[:8]  # 40 hex chars
        self.api_key = key[:64]
        return key

    def set_platforms(self, items: Iterable[str]) -> None:
        self.platforms = sorted({str(p).strip().lower() for p in (items or []) if p})

    def enable_platforms(self, items: Iterable[str]) -> None:
        s = set(self.platforms or [])
        for p in items or []:
            s.add(str(p).strip().lower())
        self.platforms = sorted(s)

    def disable_platforms(self, items: Iterable[str]) -> None:
        s = {x.lower() for x in (self.platforms or [])}
        for p in items or []:
            s.discard(str(p).strip().lower())
        self.platforms = sorted(s)

    def merge_config(self, patch: Dict[str, Any]) -> None:
        cfg = dict(self.config or {})
        for k, v in (patch or {}).items():
            cfg[k] = v
        self.config = cfg

    # --- status flows ---
    def activate(self) -> None:
        self.is_active = True
        self.is_deleted = False
        self.suspended_at = None
        self.suspend_reason = None

    def deactivate(self) -> None:
        self.is_active = False

    def soft_delete(self) -> None:
        self.is_deleted = True
        self.is_active = False

    def restore(self) -> None:
        self.is_deleted = False
        self.is_active = True

    def suspend(self, reason: str | None = None) -> None:
        self.suspended_at = _now_utc()
        self.suspend_reason = (reason or "")[:255]
        self.is_active = False

    def unsuspend(self) -> None:
        self.suspended_at = None
        self.suspend_reason = None
        self.is_active = True

    # --- quotas & usage ---
    def can_send(self, n: int = 1) -> bool:
        if n <= 0:
            return True
        if not self.is_active or self.is_deleted or self.suspended_at:
            return False
        if self.daily_quota and (self.messages_sent_today + n) > self.daily_quota:
            return False
        if self.monthly_quota and (self.messages_sent_month + n) > self.monthly_quota:
            return False
        return True

    def mark_sent(self, n: int = 1) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self.messages_sent += n
        self.messages_sent_today += n
        self.messages_sent_month += n
        self.last_used_at = _now_utc()

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<UserBot id={self.id} user={self.user_id} name='{self.name}' "
                f"active={self.is_active} deleted={self.is_deleted} pkg={self.bot_package_id}>")

    # ----------------- Validators -----------------
    @validates("name")
    def _v_name(self, _k: str, v: str) -> str:
        s = (v or "").strip()
        if len(s) < 2:
            raise ValueError("name must have at least 2 characters")
        return s[:100]

    @validates("slug")
    def _v_slug(self, _k: str, v: Optional[str]) -> Optional[str]:
        return _slugify(v)

    @validates("daily_quota", "monthly_quota", "messages_sent", "messages_sent_today", "messages_sent_month")
    def _v_nonneg(self, _k: str, v: int) -> int:
        iv = int(v or 0)
        if iv < 0:
            raise ValueError(f"{_k} must be >= 0")
        return iv

    @validates("api_key")
    def _v_api_key(self, _k: str, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        return v.strip()[:64] or None

    @validates("webhook_url")
    def _v_webhook(self, _k: str, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        return v.strip()[:255] or None


# -------- Normalization events (defensive) --------
@listens_for(UserBot, "before_insert")
def _ub_before_insert(_m, _c, t: UserBot) -> None:  # pragma: no cover
    t.name = (t.name or "").strip()[:100]
    t.slug = _slugify(t.slug)
    t.webhook_url = None if not t.webhook_url else t.webhook_url.strip()[:255]
    # normalize lists/dicts
    t.platforms = sorted({str(p).strip().lower() for p in (t.platforms or []) if p})
    t.config = dict(t.config or _dict_default())

@listens_for(UserBot, "before_update")
def _ub_before_update(_m, _c, t: UserBot) -> None:  # pragma: no cover
    _ub_before_insert(_m, _c, t)
