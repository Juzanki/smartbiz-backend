# backend/models/ai_bot_settings.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from typing import Optional, Dict, Any, List, Iterable, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.event import listens_for
from sqlalchemy.ext.mutable import MutableDict, MutableList

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

if TYPE_CHECKING:
    from .user import User


def _list_default() -> list[str]:
    # epuka mutable default kwenye class
    return []


def _dict_default() -> dict[str, Any]:
    # configs za per-channel
    return {
        "telegram": {"welcome_enabled": True, "rate_limit_per_min": 20},
        "whatsapp": {"welcome_enabled": True, "rate_limit_per_min": 15},
        "sms": {"welcome_enabled": False, "rate_limit_per_min": 10},
    }


class AIBotSettings(Base):
    """
    AIBotSettings — mipangilio ya AI assistant kwa kila mtumiaji.
    - JSON portable (PG: JSONB) na mutable (in-place updates)
    - TZ-aware timestamps
    - QoL helpers salama
    """
    __tablename__ = "ai_bot_settings"
    __mapper_args__ = {"eager_defaults": True}

    # 1:1 na User (PK = user_id)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ISO language code ('en', 'sw', 'fr', 'en-US', ...)
    language: Mapped[str] = mapped_column(String(12), default="en", nullable=False)

    default_greeting: Mapped[str] = mapped_column(
        String(255),
        default="Hello! How can I assist you today?",
        nullable=False,
        doc="Opening line for new conversations",
    )

    # Orodha ya platforms zinazoruhusiwa (mutable JSON list)
    platforms: Mapped[List[str]] = mapped_column(
        MutableList.as_mutable(JSON_VARIANT),
        default=_list_default,
        nullable=False,
        doc='e.g. ["telegram","whatsapp"]',
    )

    # Configs za kina kwa kila channel (mutable JSON obj)
    channel_config: Mapped[Dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON_VARIANT),
        default=_dict_default,
        nullable=False,
    )

    # Tabia za hiari
    auto_reply: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    typing_indicator: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Muda wa kazi (HH:MM) na timezone string
    work_hours_start: Mapped[Optional[str]] = mapped_column(String(5), default="08:00")  # HH:MM
    work_hours_end:   Mapped[Optional[str]] = mapped_column(String(5), default="22:00")  # HH:MM
    timezone:         Mapped[Optional[str]] = mapped_column(String(64), default="Africa/Dar_es_Salaam")

    # Audit
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Uhusiano na User (uselist=False kwa 1:1)
    user: Mapped["User"] = relationship(
        "User", back_populates="ai_bot_settings", lazy="selectin", uselist=False
    )

    __table_args__ = (
        Index("ix_ai_bot_settings_active", "active"),
        Index("ix_ai_bot_settings_lang", "language"),
        CheckConstraint("length(language) > 0", name="ck_ai_bot_language_nonempty"),
        CheckConstraint(
            "(work_hours_start IS NULL) OR (length(work_hours_start) = 5)",
            name="ck_ai_bot_whs_len5",
        ),
        CheckConstraint(
            "(work_hours_end IS NULL) OR (length(work_hours_end) = 5)",
            name="ck_ai_bot_whe_len5",
        ),
    )

    # ----------------- Helpers (no DB I/O) -----------------
    def set_language(self, code: str) -> None:
        self.language = (code or "en").strip()[:12]

    def set_greeting(self, text: str) -> None:
        self.default_greeting = (text or "").strip()[:255]

    def enable_platforms(self, items: Iterable[str]) -> None:
        s = {x.strip().lower() for x in (self.platforms or []) if x}
        for p in items or []:
            p = (p or "").strip().lower()
            if p:
                s.add(p)
        self.platforms = sorted(s)

    def disable_platforms(self, items: Iterable[str]) -> None:
        s = {x.strip().lower() for x in (self.platforms or []) if x}
        for p in items or []:
            s.discard((p or "").strip().lower())
        self.platforms = sorted(s)

    def set_platforms(self, items: Iterable[str]) -> None:
        self.platforms = sorted({(p or "").strip().lower() for p in (items or []) if p})

    def merge_channel_config(self, updates: Dict[str, Any]) -> None:
        cfg = dict(self.channel_config or {})
        for k, v in (updates or {}).items():
            cfg[k] = v
        self.channel_config = cfg  # MutableDict → ORM itatambua diff

    @staticmethod
    def _parse_hhmm(val: Optional[str]) -> Optional[dt.time]:
        if not val:
            return None
        try:
            h, m = map(int, val.split(":"))
            return dt.time(h, m)
        except Exception:
            return None

    def within_work_hours(self, when: Optional[dt.time] = None) -> bool:
        """
        Angalia kama muda uliotolewa (au sasa) uko ndani ya work hours.
        Inaheshimu mipaka inayovuka usiku (e.g., 22:00 → 06:00).
        """
        start_t = self._parse_hhmm(self.work_hours_start)
        end_t = self._parse_hhmm(self.work_hours_end)
        if not start_t or not end_t:
            return True  # hakuna vikwazo

        now = when or dt.datetime.now().time()
        if start_t <= end_t:
            return start_t <= now <= end_t
        # wrap-around (mfano shift ya usiku)
        return now >= start_t or now <= end_t

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AIBotSettings user_id={self.user_id} active={self.active} lang={self.language}>"

# ----------------- Normalizers / Guards -----------------

@listens_for(AIBotSettings, "before_insert")
def _ai_before_insert(_m, _c, t: AIBotSettings) -> None:
    if t.language:
        t.language = t.language.strip()[:12]
    if t.default_greeting:
        t.default_greeting = t.default_greeting.strip()[:255]
    if t.timezone:
        t.timezone = t.timezone.strip()[:64]
    # sanitize platforms (lowercase, unique)
    t.platforms = sorted({(p or "").strip().lower() for p in (t.platforms or []) if p})
    # clamp HH:MM format len is already enforced by constraints

@listens_for(AIBotSettings, "before_update")
def _ai_before_update(_m, _c, t: AIBotSettings) -> None:
    if t.language:
        t.language = t.language.strip()[:12]
    if t.default_greeting:
        t.default_greeting = t.default_greeting.strip()[:255]
    if t.timezone:
        t.timezone = t.timezone.strip()[:64]
    t.platforms = sorted({(p or "").strip().lower() for p in (t.platforms or []) if p})
