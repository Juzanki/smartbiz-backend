# backend/models/live_viewer.py
# -*- coding: utf-8 -*-
"""
Viewer (mtazamaji) wa LiveStream.

Vipengele:
- Dedupe “hai” (is_active=true) kwa (live_stream_id,user_id) AU (live_stream_id,session_key).
- Faragha ya IP: tunahifadhi ip_hash daima; client_ip huandikwa tu kama STORE_PLAIN_IP=1.
- Heartbeat na ufuatiliaji wa muda (last_seen_at, duration_seconds).
- Rel: Viewer.live_stream ↔ LiveStream.viewers (back_populates)
- Utilities: join_or_heartbeat, deactivate_others_for_identity, cleanup_stale.

Inafanya kazi na SQLAlchemy 2.x, na hujikinga na mazingira tofautitofauti
(Postgres/SQLite), pia hutoa fallbacks kama `_types.py` haipo.
"""
from __future__ import annotations

import datetime as dt
import enum
import hashlib
import hmac
import os
import re
from typing import Any, Dict, Optional, Iterable, TYPE_CHECKING

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
    and_,
    or_,
    func,
    select,
    update,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship, Session, validates

# ── Base import (layout-safe) ─────────────────────────────────────────────────
try:
    from backend.db import Base  # type: ignore
except Exception:  # pragma: no cover
    from db import Base  # type: ignore

# ── JSON type fallbacks (kama _types.py haipo) ────────────────────────────────
try:
    # Preferred: adapta yako ikiwa ipo
    from backend.models._types import JSON_VARIANT, as_mutable_json  # type: ignore
except Exception:  # pragma: no cover
    try:
        from models._types import JSON_VARIANT, as_mutable_json  # type: ignore
    except Exception:  # pragma: no cover
        # Fallback ya jumla: JSONB kwa Postgres, JSON kwa wengine
        from sqlalchemy.ext.mutable import MutableDict
        try:
            from sqlalchemy.dialects.postgresql import JSONB as _JSON_VARIANT  # type: ignore
        except Exception:  # pragma: no cover
            from sqlalchemy import JSON as _JSON_VARIANT  # type: ignore
        JSON_VARIANT = _JSON_VARIANT  # type: ignore

        def as_mutable_json(coltype):  # type: ignore
            return MutableDict.as_mutable(coltype)

if TYPE_CHECKING:
    from .user import User
    from .live_stream import LiveStream  # __tablename__ = "live_streams"

__all__ = [
    "Viewer",
    "LiveViewer",
    "ViewerPlatform",
    "cleanup_stale_viewers",
]

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
    Dedupe “hai”: (live_stream_id + user_id) AU (live_stream_id + session_key).
    """
    __tablename__ = "live_viewers"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        # Dedupe ya “hai” (is_active = true). Inaruhusu historia nyingi zisizo hai.
        UniqueConstraint("live_stream_id", "user_id", "is_active", name="uq_lv_stream_user_active"),
        UniqueConstraint("live_stream_id", "session_key", "is_active", name="uq_lv_stream_session_active"),
        # Fahirisi muhimu
        Index("ix_lv_stream_joined", "live_stream_id", "joined_at"),
        Index("ix_lv_user_joined", "user_id", "joined_at"),
        Index("ix_lv_active", "live_stream_id", "is_active", "last_seen_at"),
        Index("ix_lv_room", "room_id"),
        Index("ix_lv_last_seen", "last_seen_at"),
        Index("ix_lv_ip_hash", "ip_hash"),
        Index("ix_lv_platform", "platform"),
        # Mantiki: angalau user_id au session_key ipo; muda usirudi nyuma
        CheckConstraint("(user_id IS NOT NULL) OR (session_key IS NOT NULL)", name="ck_lv_actor_present"),
        CheckConstraint("left_at IS NULL OR joined_at IS NULL OR left_at >= joined_at", name="ck_lv_time_order"),
        {"extend_existing": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # -------- Stream target --------
    stream_id: Mapped[int] = mapped_column(
        "live_stream_id",
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Target LiveStream.id",
    )
    room_id: Mapped[Optional[str]] = mapped_column(String(120), index=True)

    # -------- Actor --------
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
        doc="NULL kama mgeni (anonymous)",
    )
    session_key: Mapped[Optional[str]] = mapped_column(
        String(64),
        index=True,
        nullable=True,
        doc="Kitambulisho cha mgeni (cookie/device).",
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
    # NB: hatutumii primaryjoin ya string ili kuepuka eval context; SQLA ita-infer join.
    live_stream: Mapped["LiveStream"] = relationship(
        "LiveStream",
        back_populates="viewers",
        foreign_keys=lambda: [Viewer.stream_id],
        passive_deletes=True,
        lazy="selectin",
    )

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

    @hybrid_property
    def identity_key(self) -> str:
        """Kifupi cha utambulisho: 'u:<id>' au 's:<session_key>'."""
        return f"u:{self.user_id}" if self.user_id is not None else f"s:{self.session_key or ''}"

    # -------- Helpers (instance) --------
    def heartbeat(self) -> None:
        self.last_seen_at = dt.datetime.now(dt.timezone.utc)

    def touch(self) -> None:
        """Huamsha tena kama alikuwa si active."""
        self.is_active = True
        self.heartbeat()

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

    def to_public_dict(self) -> Dict[str, Any]:
        """Muonekano salama kwa API/telemetry."""
        return {
            "id": self.id,
            "live_stream_id": self.stream_id,
            "room_id": self.room_id,
            "user_id": self.user_id,
            "is_anonymous": self.is_anonymous,
            "platform": self.platform.value,
            "device": self.device,
            "country": self.country,
            "city": self.city,
            "joined_at": self.joined_at.isoformat() if self.joined_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "left_at": self.left_at.isoformat() if self.left_at else None,
            "duration_seconds": self.duration_seconds,
        }

    # -------- Class utilities (session-based) --------
    @classmethod
    def _identity_filter(cls, stream_id: int, *, user_id: Optional[int], session_key: Optional[str]):
        base = (cls.stream_id == stream_id, cls.is_active.is_(True))
        if user_id is not None:
            return and_(*base, cls.user_id == user_id)
        return and_(*base, cls.session_key == session_key)

    @classmethod
    def deactivate_others_for_identity(
        cls, session: Session, *, stream_id: int, user_id: Optional[int], session_key: Optional[str]
    ) -> int:
        """Zima viewers wengine 'hai' wenye utambulisho huohuo kabla ya kuunda upya."""
        cond = cls._identity_filter(stream_id, user_id=user_id, session_key=session_key)
        res = session.execute(
            update(cls)
            .where(cond)
            .values(is_active=False, left_at=func.coalesce(cls.last_seen_at, func.now()))
        )
        return int(res.rowcount or 0)

    @classmethod
    def find_active(
        cls, session: Session, *, stream_id: int, user_id: Optional[int], session_key: Optional[str]
    ) -> Optional["Viewer"]:
        cond = cls._identity_filter(stream_id, user_id=user_id, session_key=session_key)
        return session.execute(select(cls).where(cond).limit(1)).scalar_one_or_none()

    @classmethod
    def join_or_heartbeat(
        cls,
        session: Session,
        *,
        stream_id: int,
        user_id: Optional[int] = None,
        session_key: Optional[str] = None,
        room_id: Optional[str] = None,
        platform: Optional[ViewerPlatform] = None,
        device: Optional[str] = None,
        user_agent: Optional[str] = None,
        country: Optional[str] = None,
        city: Optional[str] = None,
        referer: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        client_ip: Optional[str] = None,
    ) -> "Viewer":
        """
        - Kama active viewer yupo: heartbeat + sasisha context.
        - La sivyo: zima wengine (ikiwa wapo) ⇒ tengeneza mpya active.
        """
        if user_id is None and not session_key:
            raise ValueError("Either user_id or session_key is required")

        v = cls.find_active(session, stream_id=stream_id, user_id=user_id, session_key=session_key)
        now = dt.datetime.now(dt.timezone.utc)

        if v:
            v.last_seen_at = now
            if room_id is not None:
                v.room_id = room_id or None
            if platform is not None:
                v.platform = platform
            if device is not None:
                v.device = device or None
            if user_agent is not None:
                v.user_agent = user_agent or None
            if country is not None:
                v.country = (country or "").strip().upper() or None
            if city is not None:
                v.city = (city or "").strip() or None
            if referer is not None:
                v.referer = (referer or "").strip() or None
            if meta is not None:
                v.meta = meta or None
            if client_ip is not None:
                v.set_ip(client_ip)
            return v

        # create fresh
        cls.deactivate_others_for_identity(session, stream_id=stream_id, user_id=user_id, session_key=session_key)

        v = cls(
            stream_id=stream_id,
            user_id=user_id,
            session_key=session_key,
            room_id=room_id or None,
            platform=platform or ViewerPlatform.web,
            device=(device or None),
            user_agent=(user_agent or None),
            country=(country or "").strip().upper() or None,
            city=(city or "").strip() or None,
            referer=(referer or "").strip() or None,
            meta=meta or None,
            is_active=True,
            joined_at=now,
            last_seen_at=now,
        )
        if client_ip:
            v.set_ip(client_ip)
        session.add(v)
        return v


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


# ───────────────────────── Cleanup helper (module level) ──────────────────────
def cleanup_stale_viewers(
    session: Session,
    *,
    for_stream_id: Optional[int] = None,
    timeout_seconds: int = 60,
) -> int:
    """
    Weka inactive viewers waliopitiliza muda (last_seen_at is ‘too old’).
    Inarudisha idadi ya rows zilizoathiriwa.
    """
    now = dt.datetime.now(dt.timezone.utc)
    stale_cut = now - dt.timedelta(seconds=max(1, int(timeout_seconds)))

    conds: Iterable[Any] = [
        Viewer.is_active.is_(True),
        Viewer.last_seen_at.is_not(None),
        Viewer.last_seen_at < stale_cut,
    ]
    if for_stream_id is not None:
        conds = (*conds, Viewer.stream_id == int(for_stream_id))

    res = session.execute(
        update(Viewer)
        .where(and_(*conds))
        .values(is_active=False, left_at=func.coalesce(Viewer.last_seen_at, func.now()))
    )
    return int(res.rowcount or 0)


# ───────────────────────── Backward-compat ────────────────────────────────────
# Ruhusu import ya zamani: from backend.models.live_viewer import LiveViewer
LiveViewer = Viewer
