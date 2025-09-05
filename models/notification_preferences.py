# backend/models/notification_preferences.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, Dict, Any, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# ---------------- Enums ----------------
class DigestFrequency(str, enum.Enum):
    immediate = "immediate"   # tuma papo hapo
    hourly    = "hourly"
    daily     = "daily"
    weekly    = "weekly"
    off       = "off"         # zima zisizo critical

# ---------------- Model ----------------
class NotificationPreference(Base):
    """
    User notification preferences (production-grade):

    - Channel toggles (email/sms/push) + critical_only
    - Digest: frequency + minute-of-day + day-of-week (TZ-aware)
    - DND window: start/end minute (0..1439), supports over-midnight
    - Global mute: mute_until (except critical)
    - per_type_overrides: {"billing":{"email":true,"push":false}, ...}
    - Helpers: allows(), is_dnd_active(), next_digest_at(), mute_for(), set_dnd_window(), set_digest()
    """
    __tablename__ = "notification_preferences"
    __mapper_args__ = {"eager_defaults": True}

    __table_args__ = (
        # Uniques / indexes
        UniqueConstraint("user_id", name="uq_notifpref_user"),
        Index("ix_notifpref_critical", "enable_critical_only"),
        Index("ix_notifpref_mute_until", "mute_until"),
        Index("ix_notifpref_digest", "digest_frequency"),
        Index("ix_notifpref_dnd_enabled", "dnd_enabled", "timezone"),
        # Ranges
        CheckConstraint(
            "(digest_minute_of_day IS NULL) OR (digest_minute_of_day BETWEEN 0 AND 1439)",
            name="ck_np_digest_minute_range",
        ),
        CheckConstraint(
            "(dnd_start_minute IS NULL) OR (dnd_start_minute BETWEEN 0 AND 1439)",
            name="ck_np_dnd_start_range",
        ),
        CheckConstraint(
            "(dnd_end_minute IS NULL) OR (dnd_end_minute BETWEEN 0 AND 1439)",
            name="ck_np_dnd_end_range",
        ),
        CheckConstraint(
            "(digest_day_of_week IS NULL) OR (digest_day_of_week BETWEEN 0 AND 6)",
            name="ck_np_digest_dow_range",
        ),
        # Conditional guards (light, cross-DB):
        # hourly/daily/weekly => lazima minute; weekly => pia DOW
        CheckConstraint(
            "(digest_frequency NOT IN ('hourly','daily','weekly')) OR (digest_minute_of_day IS NOT NULL)",
            name="ck_np_digest_minute_required",
        ),
        CheckConstraint(
            "(digest_frequency <> 'weekly') OR (digest_day_of_week IS NOT NULL)",
            name="ck_np_weekly_dow_required",
        ),
        {"extend_existing": True},
    )

    # 1:1 with User (PK = FK)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    user = relationship("User", back_populates="notification_preferences", passive_deletes=True, lazy="selectin")

    # Channel toggles
    enable_email: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enable_sms:   Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enable_push:  Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Critical-only (allow only high/urgent)
    enable_critical_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # Digesting
    digest_frequency: Mapped[DigestFrequency] = mapped_column(
        SQLEnum(DigestFrequency, name="notif_digest_frequency"),
        default=DigestFrequency.immediate,
        nullable=False,
        index=True,
    )
    digest_minute_of_day: Mapped[Optional[int]] = mapped_column(Integer)  # 0..1439
    digest_day_of_week:   Mapped[Optional[int]] = mapped_column(Integer)  # 0=Mon .. 6=Sun (weekly)

    # DND window
    dnd_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    dnd_start_minute: Mapped[Optional[int]] = mapped_column(Integer)  # 0..1439
    dnd_end_minute:   Mapped[Optional[int]] = mapped_column(Integer)  # 0..1439
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)

    # Global mute (except critical)
    mute_until: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Per-type overrides and extra meta
    per_type_overrides: Mapped[Optional[Dict[str, Dict[str, bool]]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

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

    # --------- Hybrids ---------
    @hybrid_property
    def is_muted(self) -> bool:
        return bool(self.mute_until and dt.datetime.now(dt.timezone.utc) < self.mute_until)

    @hybrid_property
    def dnd_window(self) -> Optional[Tuple[int, int]]:
        if not self.dnd_enabled or self.dnd_start_minute is None or self.dnd_end_minute is None:
            return None
        return int(self.dnd_start_minute), int(self.dnd_end_minute)

    # --------- Helpers (TZ & time windows) ---------
    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone or "UTC")
        except Exception:
            return ZoneInfo("UTC")

    def _local_minutes_now(self, when: Optional[dt.datetime] = None) -> int:
        """Return minute-of-day (0..1439) in the configured timezone."""
        when = when or dt.datetime.now(dt.timezone.utc)
        local = when.astimezone(self._tz())
        return local.hour * 60 + local.minute

    def is_dnd_active(self, when: Optional[dt.datetime] = None) -> bool:
        if not self.dnd_enabled or self.dnd_start_minute is None or self.dnd_end_minute is None:
            return False
        now_min = self._local_minutes_now(when)
        start, end = int(self.dnd_start_minute), int(self.dnd_end_minute)
        if start == end:
            return False  # empty window
        if start < end:
            return start <= now_min < end
        # over-midnight window
        return now_min >= start or now_min < end

    # --------- Channel logic ---------
    def _channel_globally_enabled(self, channel: str) -> bool:
        ch = (channel or "").lower()
        return (ch == "email" and self.enable_email) or \
               (ch == "sms"   and self.enable_sms) or \
               (ch == "push"  and self.enable_push)

    def _override_allows(self, notif_type: Optional[str], channel: str) -> Optional[bool]:
        if not notif_type or not self.per_type_overrides:
            return None
        t = self.per_type_overrides.get(str(notif_type).lower())
        if not t:
            return None
        key = channel.lower()
        return bool(t[key]) if key in t else None

    def allows(
        self,
        *,
        channel: str,
        priority: str = "normal",      # "low" | "normal" | "high" | "urgent"
        notif_type: Optional[str] = None,
        when: Optional[dt.datetime] = None,
    ) -> bool:
        """
        Allow sending on a channel?
          1) Channel must be enabled (or override=true)
          2) If critical_only ? only high/urgent
          3) If DND active ? block unless high/urgent
          4) If muted ? block unless high/urgent
        """
        prio = (priority or "normal").lower()
        is_critical = prio in ("high", "urgent")

        override = self._override_allows(notif_type, channel)
        channel_ok = override if override is not None else self._channel_globally_enabled(channel)
        if not channel_ok and not is_critical:
            return False

        if self.enable_critical_only and not is_critical:
            return False
        if self.is_muted and not is_critical:
            return False
        if self.is_dnd_active(when) and not is_critical:
            return False
        return True

    # --------- Digest scheduling ---------
    def next_digest_at(self, *, after: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
        """
        Rudisha wakati ujao (UTC) wa digest kulingana na frequency na TZ.
        • immediate/off => None
        • hourly => next saa kwenye dakika iliyosetiwa
        • daily  => leo/ kesho saa:min iliyosetiwa
        • weekly => siku (0=Mon..6=Sun) saa:min
        """
        freq = self.digest_frequency
        if freq in (DigestFrequency.immediate, DigestFrequency.off):
            return None
        if self.digest_minute_of_day is None:
            return None
        tz = self._tz()
        base = (after or dt.datetime.now(dt.timezone.utc)).astimezone(tz)

        minute = int(self.digest_minute_of_day)
        target_h, target_m = divmod(minute, 60)

        def to_utc(local_dt: dt.datetime) -> dt.datetime:
            return local_dt.astimezone(dt.timezone.utc)

        if freq == DigestFrequency.hourly:
            # Next top of hour at target minute
            cand = base.replace(minute=target_m, second=0, microsecond=0)
            if base.minute >= target_m:
                cand = cand + dt.timedelta(hours=1)
            return to_utc(cand)

        if freq == DigestFrequency.daily:
            cand = base.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            if cand <= base:
                cand = cand + dt.timedelta(days=1)
            return to_utc(cand)

        if freq == DigestFrequency.weekly:
            if self.digest_day_of_week is None:
                return None
            dow = int(self.digest_day_of_week)  # 0=Mon..6=Sun
            # Python Monday=0
            base_dow = base.weekday()
            days_ahead = (dow - base_dow) % 7
            cand = base.replace(hour=target_h, minute=target_m, second=0, microsecond=0) + dt.timedelta(days=days_ahead)
            if cand <= base:
                cand = cand + dt.timedelta(days=7)
            return to_utc(cand)

        return None

    def should_digest_now(self, *, now: Optional[dt.datetime] = None, window_minutes: int = 5) -> bool:
        """
        Angalia kama tupo ndani ya dirisha la kutuma digest (kwa scheduler).
        """
        nxt = self.next_digest_at(after=(now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(minutes=window_minutes))
        if not nxt:
            return False
        now = now or dt.datetime.now(dt.timezone.utc)
        return abs((now - nxt).total_seconds()) <= window_minutes * 60

    # --------- Convenience mutators ---------
    def mute_for(self, *, minutes: int) -> None:
        self.mute_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=max(1, int(minutes)))

    def clear_mute(self) -> None:
        self.mute_until = None

    def set_dnd_window(self, *, start_minute: int, end_minute: int, enabled: bool = True) -> None:
        """Weka DND (0..1439). Ruhusu over-midnight."""
        s = max(0, min(1439, int(start_minute)))
        e = max(0, min(1439, int(end_minute)))
        self.dnd_start_minute = s
        self.dnd_end_minute = e
        self.dnd_enabled = bool(enabled)

    def set_digest(self, *, frequency: DigestFrequency, minute_of_day: Optional[int] = None, day_of_week: Optional[int] = None) -> None:
        self.digest_frequency = frequency
        if frequency in (DigestFrequency.hourly, DigestFrequency.daily, DigestFrequency.weekly):
            if minute_of_day is None:
                raise ValueError("minute_of_day is required for hourly/daily/weekly digest.")
            self.digest_minute_of_day = max(0, min(1439, int(minute_of_day)))
        else:
            self.digest_minute_of_day = None
        if frequency == DigestFrequency.weekly:
            if day_of_week is None:
                raise ValueError("day_of_week is required for weekly digest.")
            self.digest_day_of_week = max(0, min(6, int(day_of_week)))
        else:
            self.digest_day_of_week = None

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<NotificationPreference user={self.user_id} tz={self.timezone} "
                f"digest={self.digest_frequency} dnd={self.dnd_enabled} critical_only={self.enable_critical_only}>")

# ---------------- Validators / Normalizers ----------------
@validates("timezone")
def _validate_tz(_inst, _key, value: str) -> str:
    v = (value or "UTC").strip() or "UTC"
    try:
        ZoneInfo(v)
        return v
    except ZoneInfoNotFoundError:
        return "UTC"

@validates("digest_minute_of_day", "dnd_start_minute", "dnd_end_minute")
def _clamp_minutes(_inst, _key, value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    v = int(value)
    if v < 0: v = 0
    if v > 1439: v = 1439
    return v

@validates("digest_day_of_week")
def _clamp_dow(_inst, _key, value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    v = int(value)
    if v < 0: v = 0
    if v > 6: v = 6
    return v

@validates("per_type_overrides")
def _normalize_overrides(_inst, _key, value: Optional[Dict[str, Dict[str, bool]]]):
    if not value:
        return None
    clean: Dict[str, Dict[str, bool]] = {}
    for t, ch in value.items():
        if not isinstance(ch, dict):
            continue
        tkey = str(t).lower().strip()
        if not tkey:
            continue
        ch_clean: Dict[str, bool] = {}
        for k, v in ch.items():
            ch_clean[str(k).lower().strip()] = bool(v)
        if ch_clean:
            clean[tkey] = ch_clean
    return clean or None
