# backend/models/like.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method
from sqlalchemy.ext.mutable import MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT  # portable JSON/JSONB

if TYPE_CHECKING:
    from .user import User
    from .live_stream import LiveStream  # __tablename__="live_streams"


# -------- Enums --------
class Reaction(str, enum.Enum):
    like   = "like"   # classic “thumb”/heart
    love   = "love"
    fire   = "fire"
    laugh  = "laugh"
    clap   = "clap"
    wow    = "wow"
    sad    = "sad"
    angry  = "angry"
    other  = "other"

class LikeSource(str, enum.Enum):
    web     = "web"
    ios     = "ios"
    android = "android"
    tv      = "tv"
    sdk     = "sdk"
    bot     = "bot"
    other   = "other"


class Like(Base):
    """
    Reaction/Like ya mtazamaji kwenye LiveStream au room.

    Vipengele:
    • Target:  live_stream_id (FK) au room_id (string)
    • Actor:   user_id (mtumiaji) au session_key (mgeni)
    • Reaction nyingi (❤🔥😂 ...), na “weight” kuunga mkono boosts/experiments
    • Idempotency & minute_bucket kwa dedupe/analytics ya trafiki kubwa
    • Soft-unlike + sababu (unlike_reason) na anti-spam flags
    • Uhamishaji: from guest(session_key) → user_id baada ya login
    """
    __tablename__ = "likes"
    __mapper_args__ = {"eager_defaults": True}

    # ---------- Dedupe / Guards / Hot-path ----------
    __table_args__ = (
        # Dedupe kwa watumiaji waliologin (kwa reaction tofauti tunaruhusu rows tofauti)
        UniqueConstraint("live_stream_id", "user_id", "reaction", name="uq_like_stream_user_reac"),
        UniqueConstraint("room_id",        "user_id", "reaction", name="uq_like_room_user_reac"),
        # Dedupe kwa wageni (session)
        UniqueConstraint("live_stream_id", "session_key", "reaction", name="uq_like_stream_sess_reac"),
        UniqueConstraint("room_id",        "session_key", "reaction", name="uq_like_room_sess_reac"),
        # Hot-path indexes
        Index("ix_like_stream_time", "live_stream_id", "created_at"),
        Index("ix_like_user_time",   "user_id",        "created_at"),
        Index("ix_like_room_time",   "room_id",        "created_at"),
        Index("ix_like_active_reac", "is_active", "reaction"),
        Index("ix_like_bucket_stream", "minute_bucket", "live_stream_id"),
        Index("ix_like_idem", "idempotency_key"),
        # Guards
        CheckConstraint("(live_stream_id IS NOT NULL) OR (room_id IS NOT NULL)", name="ck_like_target_present"),
        CheckConstraint("(user_id IS NOT NULL) OR (session_key IS NOT NULL)",    name="ck_like_actor_present"),
        CheckConstraint("weight >= 1", name="ck_like_weight_min1"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # ---------- Target ----------
    live_stream_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
        doc="FK ya LiveStream; tumia ikiwa ipo.",
    )
    room_id: Mapped[Optional[str]] = mapped_column(
        String(120),
        index=True,
        nullable=True,
        doc="String ID ya chumba (fallback/compat).",
    )

    # ---------- Actor ----------
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    session_key: Mapped[Optional[str]] = mapped_column(
        String(64),
        index=True,
        nullable=True,
        doc="Kitambulisho cha mgeni/anonymous (cookie/device).",
    )

    # ---------- Reaction ----------
    reaction: Mapped[Reaction] = mapped_column(
        String(16), default=Reaction.like.value, nullable=False, index=True
    )
    weight: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    # ---------- State ----------
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"), index=True)
    deactivated_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    unlike_reason: Mapped[Optional[str]] = mapped_column(String(160))
    is_suspected_spam: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"), index=True)
    rate_limited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))

    # ---------- Context / Analytics ----------
    source: Mapped[Optional[str]] = mapped_column(String(16), index=True)  # LikeSource.* (string ili iwe portable)
    client_ip:  Mapped[Optional[str]]  = mapped_column(String(64))
    user_agent: Mapped[Optional[str]]  = mapped_column(String(400))
    minute_bucket: Mapped[dt.datetime] = mapped_column(  # kwa burst analytics
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(80), unique=True, index=True)

    meta: Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))  # {"app":"ios","build":"1.2.3","ab":"grpA"}

    # ---------- Timestamps ----------
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"), nullable=False
    )

    # ---------- Relationships ----------
    user: Mapped[Optional["User"]] = relationship(
        "User", back_populates="likes", foreign_keys=[user_id], passive_deletes=True, lazy="selectin"
    )
    live_stream: Mapped[Optional["LiveStream"]] = relationship(
        "LiveStream", back_populates="likes", foreign_keys=[live_stream_id], passive_deletes=True, lazy="selectin"
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def is_guest(self) -> bool:
        return self.user_id is None and self.session_key is not None

    @hybrid_property
    def target_key(self) -> str:
        return f"stream:{self.live_stream_id}" if self.live_stream_id else f"room:{self.room_id}"

    @hybrid_method
    def same_actor(self, *, user_id: int | None, session_key: str | None) -> bool:
        return (self.user_id == user_id) or (self.session_key and self.session_key == session_key)

    # ---------- Helpers ----------
    def _touch_bucket(self) -> None:
        # floor to minute; portable
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        self.minute_bucket = now

    def activate(self, *, weight: int | None = None) -> None:
        self.is_active = True
        self.deactivated_at = None
        if weight is not None and int(weight) >= 1:
            self.weight = int(weight)
        self._touch_bucket()

    def deactivate(self, *, reason: str | None = None) -> None:
        self.is_active = False
        self.deactivated_at = dt.datetime.now(dt.timezone.utc)
        self.unlike_reason = (reason or "")[:160] or None
        self._touch_bucket()

    def toggle(self) -> None:
        self.deactivate() if self.is_active else self.activate()

    def mark_spam(self, *, rate_limited: bool = False) -> None:
        self.is_suspected_spam = True
        if rate_limited:
            self.rate_limited = True

    def set_reaction(self, value: Reaction | str) -> None:
        rv = value.value if isinstance(value, Reaction) else str(value or "").strip().lower()
        self.reaction = rv if rv in {r.value for r in Reaction} else Reaction.other.value

    def set_source(self, value: LikeSource | str | None) -> None:
        if value is None:
            self.source = None
            return
        sv = value.value if isinstance(value, LikeSource) else str(value).strip().lower()
        self.source = sv if sv in {s.value for s in LikeSource} else LikeSource.other.value

    def adopt_guest_like(self, *, new_user_id: int) -> None:
        """
        Baada ya login, hamisha umiliki: session_key -> user_id.
        Hii inaruhusu kudumisha like ya mgeni kama ya mtumiaji.
        """
        self.user_id = new_user_id
        self.session_key = None

    # ---------- Validators ----------
    @validates("room_id")
    def _v_room(self, _k, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None

    @validates("session_key")
    def _v_sess(self, _k, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None

    @validates("weight")
    def _v_weight(self, _k, v: int) -> int:
        iv = int(v or 1)
        return 1 if iv < 1 else iv

    def __repr__(self) -> str:  # pragma: no cover
        tgt = self.live_stream_id or self.room_id
        who = self.user_id or self.session_key
        return f"<Like id={self.id} target={tgt} user={who} reac={self.reaction} w={self.weight} active={self.is_active}>"
