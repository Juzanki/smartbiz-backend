# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import hashlib
import re
import datetime as dt
from typing import Any, Dict, Optional, List, TYPE_CHECKING, Tuple

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

if TYPE_CHECKING:
    from .user import User  # type hints only


# ------------------------------ Utilities & Mixins ------------------------------ #

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")
_LANG_RE = re.compile(r"^[a-z]{2,8}([_-][A-Za-z0-9]{2,8})*$")
_CCY_RE = re.compile(r"^[A-Z]{3}|[A-Z]{2,8}$")
_TZ_RE = re.compile(r"^[A-Za-z_/\-+0-9]{2,64}$")  # lightweight guard only


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def touch(self) -> None:
        """Manual bump for updated_at when only in-memory JSON changes occur."""
        self.updated_at = _utcnow()


# --------------------------------- Enumerations -------------------------------- #

class ThemeMode(str, enum.Enum):
    light = "light"
    dark = "dark"
    system = "system"


class Platform(str, enum.Enum):
    android = "android"
    ios = "ios"
    web = "web"
    desktop = "desktop"


class MediaQuality(str, enum.Enum):
    auto = "auto"
    low = "low"
    medium = "medium"
    high = "high"


class SettingsEntity(str, enum.Enum):
    global_ = "global"
    user = "user"
    device = "device"
    notification = "notification"
    flag = "flag"


# ----------------------- Global key/value settings (app_) ----------------------- #

class Setting(Base, TimestampMixin):
    """Global configurations."""
    __tablename__ = "app_settings"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("namespace", "key", name="uq_app_settings_namespace_key"),
        Index("ix_app_settings_namespace", "namespace"),
        CheckConstraint("length(namespace) BETWEEN 1 AND 100", name="ck_app_settings_ns_len"),
        CheckConstraint("length(key) BETWEEN 1 AND 100", name="ck_app_settings_key_len"),
        {"extend_existing": True, "comment": "Global KV + JSON settings"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    namespace: Mapped[str] = mapped_column(String(100), default="default", nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[Optional[str]] = mapped_column(String(1024))
    value_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    @hybrid_property
    def value_typed(self) -> Any:
        if self.value_json is not None:
            return self.value_json
        if self.value is None:
            return None
        v = self.value.strip()
        if v.lower() in {"true", "false"}:
            return v.lower() == "true"
        try:
            return float(v) if "." in v else int(v)
        except Exception:
            return v

    def set_value(self, value: Any) -> None:
        if isinstance(value, (dict, list)):
            self.value_json = value
            self.value = None
        else:
            self.value = None if value is None else str(value)
            self.value_json = None
        self.version = int(self.version or 1) + 1
        self.touch()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "namespace": self.namespace, "key": self.key,
            "value": self.value, "value_json": self.value_json,
            "is_public": self.is_public, "version": self.version,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def upsert(
        cls,
        session,
        *,
        namespace: str,
        key: str,
        value: Any,
        description: Optional[str] = None,
        is_public: Optional[bool] = None,
    ) -> "Setting":
        obj: Optional[Setting] = session.query(cls).filter_by(
            namespace=namespace, key=key
        ).one_or_none()
        if not obj:
            obj = cls(namespace=namespace, key=key)
            session.add(obj)
        if description is not None:
            obj.description = description
        if is_public is not None:
            obj.is_public = bool(is_public)
        obj.set_value(value)
        return obj

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Setting ns={self.namespace!r} key={self.key!r} v{self.version}>"


# ----------------------- Per-user settings (app_) ----------------------- #

class UserSettings(Base, TimestampMixin):
    """User-level configuration. Exactly one row per user."""
    __tablename__ = "app_user_settings"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_app_user_settings_user_id"),
        Index("ix_app_user_settings_language", "language"),  # <- jina thabiti hapa
        CheckConstraint("length(language) BETWEEN 2 AND 16", name="ck_app_user_settings_lang_len"),
        CheckConstraint("length(currency) BETWEEN 2 AND 8", name="ck_app_user_settings_currency_len"),
        CheckConstraint("length(timezone) BETWEEN 2 AND 64", name="ck_app_user_settings_tz_len"),
        CheckConstraint("length(primary_color) BETWEEN 3 AND 16", name="ck_app_user_settings_primary_len"),
        CheckConstraint("length(secondary_color) BETWEEN 3 AND 16", name="ck_app_user_settings_secondary_len"),
        {"extend_existing": True, "comment": "1:1 user profile preferences"},
    )

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    business_name: Mapped[str] = mapped_column(String(200), nullable=False)
    tagline: Mapped[Optional[str]] = mapped_column(String(240))

    # NOTE: TUMEONDOA index=True hapa ili kuepuka duplicate na kuacha Index() ya __table_args__
    language: Mapped[str] = mapped_column(String(16), default="en", nullable=False)

    logo_url: Mapped[Optional[str]] = mapped_column(String(500))
    primary_color: Mapped[str] = mapped_column(String(16), default="#0d6efd", nullable=False)
    secondary_color: Mapped[str] = mapped_column(String(16), default="#6c757d", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Dar_es_Salaam", nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="TZS", nullable=False)
    enable_custom_domain: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    theme_mode: Mapped[ThemeMode] = mapped_column(
        SQLEnum(ThemeMode, name="app_theme_mode", native_enum=False, validate_strings=True),
        default=ThemeMode.system, nullable=False, index=True
    )
    date_format: Mapped[str] = mapped_column(String(32), default="yyyy-MM-dd", nullable=False)
    number_format: Mapped[str] = mapped_column(String(16), default="1,234.56", nullable=False)
    locale: Mapped[str] = mapped_column(String(16), default="en_US", nullable=False)

    cover_url: Mapped[Optional[str]] = mapped_column(String(500))
    contact_email: Mapped[Optional[str]] = mapped_column(String(160))
    contact_phone: Mapped[Optional[str]] = mapped_column(String(64))

    preferences: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # fully-qualified avoids registry ambiguity
    user: Mapped["User"] = relationship(
        "backend.models.user.User", back_populates="settings", uselist=False, passive_deletes=True
    )

    @validates("primary_color", "secondary_color")
    def _validate_color(self, _, value: str) -> str:
        if not value:
            raise ValueError("color cannot be empty")
        if not _HEX_RE.match(value):
            raise ValueError(f"invalid HEX color: {value!r}")
        return value if value.startswith("#") else f"#{value}"

    @validates("language")
    def _validate_language(self, _, value: str) -> str:
        if not _LANG_RE.match(value):
            raise ValueError(f"invalid language tag: {value!r}")
        return value

    @validates("currency")
    def _validate_currency(self, _, value: str) -> str:
        if not _CCY_RE.match(value):
            raise ValueError(f"invalid currency code: {value!r}")
        return value

    @validates("timezone")
    def _validate_tz(self, _, value: str) -> str:
        if not _TZ_RE.match(value):
            raise ValueError(f"invalid timezone: {value!r}")
        return value

    @hybrid_property
    def theme_colors(self) -> Dict[str, str]:
        return {"primary": self.primary_color, "secondary": self.secondary_color}

    def update_preferences(self, **patch: Any) -> None:
        p = dict(self.preferences or {})
        p.update({k: v for k, v in patch.items()})
        self.preferences = p
        self.touch()

    @classmethod
    def get_or_create(cls, session, *, user_id: int, **defaults: Any) -> "UserSettings":
        obj: Optional[UserSettings] = session.query(cls).filter_by(user_id=user_id).one_or_none()
        if obj:
            return obj
        obj = cls(user_id=user_id, **defaults)
        session.add(obj)
        return obj

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "language": self.language,
            "currency": self.currency,
            "timezone": self.timezone,
            "theme_mode": self.theme_mode.value,
            "colors": self.theme_colors,
            "preferences": self.preferences,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UserSettings user_id={self.user_id} theme={self.theme_mode.value!r} lang={self.language!r}>"


# ----------------------- Per-user KV overrides (app_) ----------------------- #

class UserKVSetting(Base, TimestampMixin):
    """Per-user key/value overrides with validity windows."""
    __tablename__ = "app_user_kv_settings"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("user_id", "namespace", "key", name="uq_app_user_kv_ns_key"),
        Index("ix_app_user_kv_user_ns", "user_id", "namespace"),
        CheckConstraint("length(namespace) BETWEEN 1 AND 100", name="ck_app_user_kv_ns_len"),
        CheckConstraint("length(key) BETWEEN 1 AND 100", name="ck_app_user_kv_key_len"),
        {"extend_existing": True, "comment": "Per-user KV overrides"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    namespace: Mapped[str] = mapped_column(String(100), default="default", nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)

    value_str: Mapped[Optional[str]] = mapped_column(String(1024))
    value_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    effective_from: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    effective_to: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    user: Mapped["User"] = relationship(
        "backend.models.user.User", back_populates="kv_settings", passive_deletes=True
    )

    @hybrid_property
    def value_typed(self) -> Any:
        return self.value_json if self.value_json is not None else self.value_str

    @hybrid_property
    def is_active(self) -> bool:
        now = _utcnow()
        if self.effective_from and now < self.effective_from:
            return False
        if self.effective_to and now > self.effective_to:
            return False
        return True

    def set_value(self, value: Any) -> None:
        if isinstance(value, (dict, list)):
            self.value_json = value
            self.value_str = None
        else:
            self.value_str = None if value is None else str(value)
            self.value_json = None
        self.version = int(self.version or 1) + 1
        self.touch()

    @classmethod
    def upsert(
        cls,
        session,
        *,
        user_id: int,
        namespace: str,
        key: str,
        value: Any,
        is_secret: Optional[bool] = None,
        effective_from: Optional[dt.datetime] = None,
        effective_to: Optional[dt.datetime] = None,
    ) -> "UserKVSetting":
        obj: Optional[UserKVSetting] = session.query(cls).filter_by(
            user_id=user_id, namespace=namespace, key=key
        ).one_or_none()
        if not obj:
            obj = cls(user_id=user_id, namespace=namespace, key=key)
            session.add(obj)
        if is_secret is not None:
            obj.is_secret = bool(is_secret)
        obj.effective_from = effective_from
        obj.effective_to = effective_to
        obj.set_value(value)
        return obj

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UserKVSetting user={self.user_id} ns={self.namespace!r} key={self.key!r} v{self.version}>"


# -------------------------- Per-device settings (app_) ------------------------- #

class UserDeviceSetting(Base, TimestampMixin):
    """Device-level preferences and capabilities for a user."""
    __tablename__ = "app_user_device_settings"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("user_id", "device_id", name="uq_app_user_device"),
        Index("ix_app_user_device_user", "user_id"),
        Index("ix_app_user_device_platform", "platform", "user_id"),
        {"extend_existing": True, "comment": "Per-user device capability/preferences"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[Platform] = mapped_column(
        SQLEnum(Platform, name="app_platform", native_enum=False, validate_strings=True),
        nullable=False, index=True
    )
    app_version: Mapped[Optional[str]] = mapped_column(String(32))
    push_token: Mapped[Optional[str]] = mapped_column(String(512))

    data_saver: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    media_quality: Mapped[MediaQuality] = mapped_column(
        SQLEnum(MediaQuality, name="app_media_quality", native_enum=False, validate_strings=True),
        default=MediaQuality.auto, nullable=False, index=True
    )
    autoplay_videos: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    haptics: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    battery_saver: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    overrides: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    last_seen_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    user: Mapped["User"] = relationship(
        "backend.models.user.User",
        back_populates="device_settings",
        foreign_keys=[user_id],
        passive_deletes=True,
    )

    def touch_seen(self) -> None:
        self.last_seen_at = _utcnow()

    def enable_push(self, token: Optional[str]) -> None:
        self.push_token = token

    def disable_push(self) -> None:
        self.push_token = None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UserDeviceSetting user={self.user_id} device={self.device_id!r} platform={self.platform.value!r}>"


# --------------------- Do Not Disturb windows (app_) --------------------- #

class DoNotDisturbWindow(Base, TimestampMixin):
    """Quiet hours windows per user."""
    __tablename__ = "app_dnd_windows"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_app_dnd_user", "user_id"),
        CheckConstraint("minutes_start BETWEEN 0 AND 1439", name="ck_app_dnd_start_bounds"),
        CheckConstraint("minutes_end BETWEEN 0 AND 1439", name="ck_app_dnd_end_bounds"),
        {"extend_existing": True, "comment": "Do Not Disturb windows per user"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # explicit FK to notification setting (optional link)
    notification_setting_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("app_notification_settings.id", ondelete="CASCADE"),
        nullable=True, index=True
    )

    name: Mapped[Optional[str]] = mapped_column(String(64))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)

    minutes_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    minutes_end: Mapped[int] = mapped_column(Integer, nullable=False, default=360)
    days_of_week: Mapped[Optional[List[int]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped["User"] = relationship(
        "backend.models.user.User",
        back_populates="dnd_windows",
        foreign_keys=[user_id],
        passive_deletes=True,
    )

    notification_setting: Mapped[Optional["NotificationSetting"]] = relationship(
        "NotificationSetting",
        back_populates="dnd_windows",
        foreign_keys=[notification_setting_id],
        passive_deletes=True,
    )

    def minutes_now(self, now: Optional[dt.datetime] = None) -> int:
        n = now or _utcnow()
        return n.hour * 60 + n.minute

    def is_quiet_now(self, now_local: Optional[dt.datetime] = None) -> bool:
        if not self.enabled:
            return False
        n = now_local or _utcnow()
        if self.days_of_week and n.weekday() not in set(self.days_of_week):
            return False
        m = self.minutes_now(n)
        if self.minutes_start <= self.minutes_end:
            return self.minutes_start <= m <= self.minutes_end
        # window wraps past midnight
        return m >= self.minutes_start or m <= self.minutes_end

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DND user={self.user_id} {self.minutes_start}-{self.minutes_end} tz={self.timezone} enabled={self.enabled}>"


# ---------------------- Notification preferences (app_) ---------------------- #

class NotificationSetting(Base, TimestampMixin):
    """Per-user notification matrix (channels + categories)."""
    __tablename__ = "app_notification_settings"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_app_notification_user"),
        {"extend_existing": True, "comment": "Per-user notification switches"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # channels
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # categories
    marketing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    product_updates: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    security: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    live_alerts: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    order_updates: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    goals: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    gifts: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    leaderboard: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mentions: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    extras: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    user: Mapped["User"] = relationship(
        "backend.models.user.User",
        back_populates="notification_setting",
        uselist=False,
        foreign_keys=[user_id],
        passive_deletes=True,
    )

    dnd_windows: Mapped[List["DoNotDisturbWindow"]] = relationship(
        "DoNotDisturbWindow",
        back_populates="notification_setting",
        foreign_keys="DoNotDisturbWindow.notification_setting_id",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @hybrid_property
    def any_channel_enabled(self) -> bool:
        return bool(self.push_enabled or self.email_enabled or self.sms_enabled)

    def is_allowed(self, *, category: str, channel: str, now_local: Optional[dt.datetime] = None) -> bool:
        if channel == "push" and not self.push_enabled:
            return False
        if channel == "email" and not self.email_enabled:
            return False
        if channel == "sms" and not self.sms_enabled:
            return False
        if hasattr(self, category) and isinstance(getattr(self, category), bool):
            if not getattr(self, category):
                return False
        for win in self.dnd_windows or []:
            if win.is_quiet_now(now_local):
                return False
        return True

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NotificationSetting user={self.user_id} push={self.push_enabled} email={self.email_enabled}>"


# -------------------------- Feature flags (app_) -------------------------- #

class FeatureFlag(Base, TimestampMixin):
    """Experimentation and staged rollouts."""
    __tablename__ = "app_feature_flags"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("key", name="uq_app_feature_flag_key"),
        Index("ix_app_feature_flags_enabled", "enabled"),
        CheckConstraint("rollout_percentage BETWEEN 0 AND 100", name="ck_app_flag_rollout_bounds"),
        {"extend_existing": True, "comment": "Feature flag definitions"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    rollout_percentage: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    audience_filters: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    starts_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    @hybrid_property
    def is_window_open(self) -> bool:
        now = _utcnow()
        if self.starts_at and now < self.starts_at:
            return False
        if self.ends_at and now > self.ends_at:
            return False
        return True

    def _bucket_for(self, stable_key: str) -> int:
        h = hashlib.sha1((stable_key or "").encode("utf-8")).hexdigest()
        return int(h[:8], 16) % 100

    def in_rollout_for(self, stable_key: str) -> bool:
        if self.rollout_percentage >= 100:
            return True
        if self.rollout_percentage <= 0:
            return False
        return self._bucket_for(stable_key) < int(self.rollout_percentage)

    def matches_audience(self, attrs: Optional[Dict[str, Any]] = None) -> bool:
        if not self.audience_filters:
            return True
        if not attrs:
            return False
        for k, v in (self.audience_filters or {}).items():
            if attrs.get(k) != v:
                return False
        return True

    def is_active_for(self, stable_key: str, attrs: Optional[Dict[str, Any]] = None) -> bool:
        return bool(
            self.enabled
            and self.is_window_open
            and self.in_rollout_for(stable_key)
            and self.matches_audience(attrs)
        )

    def active_status_for(self, stable_key: str, attrs: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """Returns (active, reason) for clearer debugging."""
        if not self.enabled:
            return False, "disabled"
        if not self.is_window_open:
            return False, "outside_schedule"
        if not self.in_rollout_for(stable_key):
            return False, "bucket_excluded"
        if not self.matches_audience(attrs):
            return False, "audience_mismatch"
        return True, "ok"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FeatureFlag key={self.key!r} enabled={self.enabled} rollout={self.rollout_percentage}%>"


class FeatureFlagOverride(Base, TimestampMixin):
    """Per-user override for a feature flag."""
    __tablename__ = "app_feature_flag_overrides"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("user_id", "flag_key", name="uq_app_flag_override_user_flag"),
        Index("ix_app_flag_override_user", "user_id"),
        {"extend_existing": True, "comment": "Per-user feature flag overrides"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    flag_key: Mapped[str] = mapped_column(String(100), nullable=False)
    force_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    user: Mapped["User"] = relationship(
        "backend.models.user.User", back_populates="flag_overrides", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FeatureFlagOverride user={self.user_id} flag={self.flag_key!r} enabled={self.force_enabled}>"


# ------------------------------- Audit trail (app_) ------------------------------ #

class SettingsAudit(Base, TimestampMixin):
    """Immutable audit trail for all configuration changes."""
    __tablename__ = "app_settings_audit"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_app_settings_audit_entity", "entity_type", "entity_id", "created_at"),
        {"extend_existing": True, "comment": "Audit log of settings changes"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    entity_type: Mapped[SettingsEntity] = mapped_column(
        SQLEnum(SettingsEntity, name="app_settings_entity_type", native_enum=False, validate_strings=True),
        nullable=False,
        index=True,
    )
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)  # create, update, delete
    diff: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    note: Mapped[Optional[str]] = mapped_column(Text)

    actor: Mapped[Optional["User"]] = relationship(
        "backend.models.user.User",
        back_populates="settings_audits",
        foreign_keys=[actor_user_id],
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SettingsAudit type={self.entity_type.value!r} entity={self.entity_id!r} action={self.action!r}>"


# ---------------------------------- Exports ---------------------------------- #

__all__ = [
    "Setting",
    "UserSettings",
    "UserKVSetting",
    "UserDeviceSetting",
    "DoNotDisturbWindow",
    "NotificationSetting",
    "FeatureFlag",
    "FeatureFlagOverride",
    "SettingsAudit",
    "ThemeMode",
    "Platform",
    "MediaQuality",
    "SettingsEntity",
    "TimestampMixin",
]
