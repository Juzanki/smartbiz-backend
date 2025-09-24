# backend/models/live_viewer.py
# -*- coding: utf-8 -*-
"""
Viewer (mtazamaji) wa LiveStream.

- Entry moja "hai" (is_active=true) huruhusiwa kwa:
    (live_stream_id, user_id) AU (live_stream_id, session_key)
- Faragha ya IP: tunahifadhi daima ip_hash; client_ip huhifadhiwa tu
  ikiwa STORE_PLAIN_IP=true
- Heartbeat: last_seen_at + helpers (heartbeat, mark_inactive_if_stale)
- Rel: Viewer.live_stream ↔ LiveStream.viewers (back_populates)

NB: Tumia canonical import ya models ('backend.models' au alias yake)
ili kuepuka double-mapping ya ORM.
"""
from __future__ import annotations

import datetime as dt
import enum
import hashlib
import hmac
import os
import re
from typing import Any, Dict, Optional, TYPE_CHECKING

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
    func,
)
from sqlalchemy.event import listens_for
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

# ── Layout-safe Base import ────────────────────────────────────────────────────
try:
    from backend.db import Base  # type: ignore
except Exception:  # pragma: no cover
    from db import Base  # type: ignore

# JSON helper (layout-safe)
try:
    from backend.models._types import JSON_VARIANT, as_mutable_json  # type: ignore
except Exception:  # pragma: no cover
    from models._types import JSON_VARIANT, as_mutable_json  # type: ignore

if TYPE_CHECKING:
    from .user import User
    from .live_stream import LiveStream  # __tablename__ = "live_streams"

__all__ = ["Viewer", "LiveViewer"]

# ───────────────────────── Enums & regex ──────────────────────────────────────
class ViewerPlatform(str, enum.Enum):
    web = "web"
    android = "android"
    ios = "ios"
    tv = "tv"
    other = "other"


SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9\-._]{8,64}$")
SAFE_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


# ───────────────────────── Model ──────────────────────────────────────────────
class Viewer(Base):
    """
    Mtazamaji wa LiveStream (anaweza kuwa user au mgeni).
    Dedupe "hai": (live_stream_id + user_id) AU (live_stream_id + session_key).
    """
    __tablename__ = "live_viewers"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        # Dedupe ya "hai"
        UniqueConstraint("live_stream_id", "user_id", "is_active", name="uq_lv_stream_user_active"),
        UniqueConstraint("live_stream_id", "session_key", "is_active", name="uq_lv_stream_session_active"),
        # Kawaida
        Index("ix_lv_stream_joined", "live_stream_id", "joined_at"),
        Index("ix_lv_user_joined", "user_id", "joined_at"),
        Index("ix_lv_active", "live_stream_id", "is_active", "last_seen_at"),
        Index("ix_lv_room", "room_id"),
        Index("ix_lv_last_seen", "last_seen_at"),
        Index("ix_lv_ip_hash", "ip_hash"),
        Index("ix_lv_platform", "platform"),
        # Ulinzi wa mantiki
        CheckConstraint("(user_id IS NOT NULL) OR (session_key IS NOT NULL)", name="ck_lv_actor_present"),
        CheckConstraint("left_at IS NULL OR joined_at IS NULL OR left_at >= joined_at", name="ck_lv_time_order"),
        {"extend_existing": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # -------- Stream target --------
    # NOTE: Attribute ni stream_id lakini DB column ni 'live_stream_id'
    stream_id: Mapped[int] = mapped_column(
        "live_stream_id",
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    room_id: Mapped[Optional[str]] = mapped_column(String(120), index=True)

    # -------- Actor --------
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
        doc="NULL kama ni mgeni (anonymous)",
    )
    session_key: Mapped[Optional[str]] = mapped_column(
        String(64),
        index=True,
        nullable=True,
        doc="Kitambulisho cha mgeni (cookie/device)",
    )

    # -------- Context / Privacy --------
    platform: Mapped[ViewerPlatform] = mapped_column(
        SQLEnum(ViewerPlatform, name="viewer_platform", native_enum=False, validate_strings=True),
        default=ViewerPlatform.web,
        nullable=False,
        index=True,
    )
    device: Mapped[Optional[str]] = mapped_column(String(100))
    client_ip: Mapped[Optional[str]] = mapped_column(String(64))
    ip_hash: Mapped[Optional[str]] = mapped_column(String(128), index=True)  # sha256 hex
    user_agent: Mapped[Optional[str]] = mapped_column(String(400))
    country: Mapped[Optional[str]] = mapped_column(String(2))  # ISO-3166-1 alpha-2
    city: Mapped[Optional[str]] = mapped_column(String(80))
    referer: Mapped[Optional[str]] = mapped_column(String(512))
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # -------- Lifecycle --------
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    last_seen_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    left_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # -------- Relationships --------
    # MUHIMU: Jina la attr hapa ni 'live_stream' ili lilingane na LiveStream.viewers(back_populates="live_stream")
    live_stream: Mapped["LiveStream"] = relationship(
        "LiveStream",
        back_populates="viewers",
        primaryjoin="Viewer.stream_id == foreign(LiveStream.id)",
        foreign_keys=lambda: [Viewer.stream_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # Kwa User hatuhitaji back_populates maalum (si lazima kuwa na User.viewers)
    user: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=lambda: [Viewer.user_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # -------- Hybrids --------
    @hybrid_property
    def duration_seconds(self) -> int:
        if not self.joined_at:
            return 0
        end = self.left_at or dt.datetime.now(dt.timezone.utc)
        return max(0, int((end - self.joined_at).total_seconds()))

    @duration_seconds.expression
    def duration_seconds(cls):
        return func.cast(
            func.extract("epoch", func.coalesce(cls.left_at, func.now()) - cls.joined_at),
            Integer,
        )

    @hybrid_property
    def is_anonymous(self) -> bool:
        return self.user_id is None and bool(self.session_key)

    # -------- Helpers --------
    def heartbeat(self) -> None:
        self.last_seen_at = dt.datetime.now(dt.timezone.utc)

    def leave(self) -> None:
        if not self.left_at:
            self.left_at = dt.datetime.now(dt.timezone.utc)
        self.is_active = False

    def mark_inactive_if_stale(self, *, timeout_seconds: int = 60) -> bool:
        if self.is_active and self.last_seen_at:
            now = dt.datetime.now(dt.timezone.utc)
            if (now - self.last_seen_at).total_seconds() > timeout_seconds:
                self.is_active = False
                self.left_at = self.left_at or self.last_seen_at or now
                return True
        return False

    # -------- Security / privacy utilities --------
    def set_ip(self, ip: Optional[str]) -> None:
        if ip:
            self.ip_hash = _hash_ip(ip)
            if os.getenv("STORE_PLAIN_IP", "0").strip().lower() in {"1", "true", "yes", "on"}:
                self.client_ip = ip
            else:
                self.client_ip = None
        else:
            self.ip_hash = None
            self.client_ip = None

    def __repr__(self) -> str:  # pragma: no cover
        who = self.user_id if self.user_id is not None else f"anon:{self.session_key}"
        return f"<Viewer id={self.id} stream={self.stream_id} who={who} active={self.is_active}>"

# ───────────────────── Validators / normalizers ───────────────────────────────
@validates("session_key")
def _normalize_session_key(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    if not SAFE_SESSION_RE.match(v):
        raise ValueError("session_key must be 8–64 chars [A-Za-z0-9-._].")
    return v

@validates("country")
def _normalize_country(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip().upper()
    if v and not SAFE_COUNTRY_RE.match(v):
        return None
    return v

@validates("user_agent", "device", "city", "referer")
def _trim_texts(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    return v or None

# ───────────────────────── Internal helpers ───────────────────────────────────
def _hash_ip(ip: str) -> str:
    """Hash IP kwa sha256 + optional pepper kupitia env IP_HASH_SECRET."""
    secret = (os.getenv("IP_HASH_SECRET") or "").encode("utf-8")
    data = ip.encode("utf-8")
    return hmac.new(secret, data, hashlib.sha256).hexdigest() if secret else hashlib.sha256(data).hexdigest()

# ───────────────────────── Event hooks ────────────────────────────────────────
@listens_for(Viewer, "before_insert")
def _lv_before_insert(_mapper, _conn, t: Viewer) -> None:  # pragma: no cover
    if t.country:
        t.country = t.country.strip().upper() or None
    if t.user_agent:
        t.user_agent = t.user_agent.strip() or None
    if t.device:
        t.device = t.device.strip() or None
    if t.referer:
        t.referer = t.referer.strip() or None
    if t.client_ip and not t.ip_hash:
        t.ip_hash = _hash_ip(t.client_ip)
        if os.getenv("STORE_PLAIN_IP", "0").strip().lower() not in {"1", "true", "yes", "on"}:
            t.client_ip = None

@listens_for(Viewer, "before_update")
def _lv_before_update(_mapper, _conn, t: Viewer) -> None:  # pragma: no cover
    if t.country:
        t.country = t.country.strip().upper() or None
    if t.user_agent:
        t.user_agent = t.user_agent.strip() or None
    if t.device:
        t.device = t.device.strip() or None
    if t.referer:
        t.referer = t.referer.strip() or None
    if t.client_ip and not t.ip_hash:
        t.ip_hash = _hash_ip(t.client_ip)
        if os.getenv("STORE_PLAIN_IP", "0").strip().lower() not in {"1", "true", "yes", "on"}:
            t.client_ip = None

# ───────────────────────── Backward-compat ────────────────────────────────────
# Ruhusu import ya zamani: from backend.models.live_viewer import LiveViewer
LiveViewer = Viewer
