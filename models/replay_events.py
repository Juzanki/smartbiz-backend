# backend/models/replay_event.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import hashlib
import re
import datetime as dt
from typing import Optional, TYPE_CHECKING, Dict, Any

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
    func,
    Float,
    text as sa_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    from .video_post import VideoPost

_TS_RE = re.compile(r"^(?P<h>\d{1,2}):(?P<m>[0-5]?\d):(?P<s>[0-5]?\d)(?:\.(?P<ms>\d{1,3}))?$")

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _hhmmss_to_seconds(s: str) -> float:
    s = (s or "").strip()
    if not s:
        return 0.0
    m = _TS_RE.match(s)
    if not m:
        parts = s.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return 0.0
    h = int(m.group("h")); mi = int(m.group("m")); sec = int(m.group("s"))
    ms = m.group("ms")
    base = h * 3600 + mi * 60 + sec
    return float(base) + (int(ms) / 1000.0 if ms else 0.0)

def _seconds_to_hhmmss(x: float) -> str:
    total_ms = max(0, int(round((x or 0.0) * 1000)))
    s, ms = divmod(total_ms, 1000)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    base = f"{h:02d}:{m:02d}:{s:02d}"
    return f"{base}.{ms:03d}" if ms else base

class ReplayEventType(str, enum.Enum):
    like     = "like"
    comment  = "comment"
    reaction = "reaction"

class ReactionKind(str, enum.Enum):
    like  = "like"
    love  = "love"
    haha  = "haha"
    wow   = "wow"
    sad   = "sad"
    angry = "angry"
    other = "other"

class ReplayEvent(Base):
    __tablename__ = "replay_events"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_replay_event_idem"),
        UniqueConstraint("fingerprint",  name="uq_replay_event_fpr"),
        Index("ix_replay_event_video_pos", "video_post_id", "position_seconds"),
        Index("ix_replay_event_video_created", "video_post_id", "created_at"),
        Index("ix_replay_event_type", "event_type"),
        Index("ix_replay_event_user", "action_user_id"),
        Index("ix_replay_event_country", "country"),
        Index("ix_replay_event_kind_time", "event_type", "reaction", "created_at"),
        CheckConstraint("position_seconds >= 0.0", name="ck_replay_event_pos_nonneg"),
        CheckConstraint(
            "(event_type <> 'comment') OR (content IS NOT NULL AND length(content) > 0)",
            name="ck_replay_event_comment_has_text",
        ),
        CheckConstraint(
            "(event_type <> 'reaction') OR (reaction IS NOT NULL)",
            name="ck_replay_event_reaction_present",
        ),
        CheckConstraint("country IS NULL OR length(country) = 2", name="ck_replay_event_country_iso2"),
    )

    # --- Identity ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # --- Target video ---
    video_post_id: Mapped[int] = mapped_column(
        ForeignKey("video_posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_post: Mapped["VideoPost"] = relationship(
        "VideoPost", back_populates="replay_events", lazy="selectin", passive_deletes=True
    )

    # --- Actor (nullable) ---
    action_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # IMPORTANT: specify foreign_keys to avoid ambiguity with moderated_by_user_id
    action_user: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[action_user_id],
        lazy="selectin",
        passive_deletes=True,
        # back_populates can be added if you have it on User side, e.g. "replay_actions"
    )

    # --- Event data ---
    event_type: Mapped[ReplayEventType] = mapped_column(
        SQLEnum(ReplayEventType, name="replay_event_type", native_enum=False, validate_strings=True),
        nullable=False,
        index=True,
    )
    reaction: Mapped[Optional[ReactionKind]] = mapped_column(
        SQLEnum(ReactionKind, name="replay_reaction_kind", native_enum=False, validate_strings=True),
        nullable=True,
        index=True,
    )
    content: Mapped[Optional[str]] = mapped_column(Text)

    # --- Threading (comment replies) ---
    parent_event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("replay_events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parent_event: Mapped[Optional["ReplayEvent"]] = relationship(
        "ReplayEvent",
        remote_side=lambda: [ReplayEvent.id],
        lazy="selectin",
    )
    # children list (no backref on parent to avoid automagic ambiguity)
    children: Mapped[list["ReplayEvent"]] = relationship(
        "ReplayEvent",
        primaryjoin=lambda: ReplayEvent.parent_event_id == ReplayEvent.id,
        viewonly=True,
        lazy="selectin",
    )

    # --- Position in video ---
    position_seconds: Mapped[float] = mapped_column(Float, nullable=False, server_default=sa_text("0"))
    timestamp_str:   Mapped[Optional[str]] = mapped_column(String(16), index=True)  # HH:MM:SS(.mmm)

    # --- Context ---
    platform:   Mapped[Optional[str]] = mapped_column(String(32), index=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(String(400))
    country:    Mapped[Optional[str]] = mapped_column(String(2))  # ISO-3166 alpha-2

    # --- Idempotency / metadata ---
    fingerprint:     Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    request_id:      Mapped[Optional[str]] = mapped_column(String(64), index=True)
    meta:            Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # --- Moderation ---
    is_hidden:   Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_deleted:  Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_reported: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    mod_reason:  Mapped[Optional[str]] = mapped_column(String(160))
    deleted_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    hidden_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    moderated_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    moderated_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[moderated_by_user_id],
        lazy="selectin",
        passive_deletes=True,
        # back_populates can be added if you have it on User side, e.g. "replay_moderated"
    )

    # --- Timestamps ---
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def hhmmss(self) -> str:
        return self.timestamp_str or _seconds_to_hhmmss(self.position_seconds or 0.0)

    # ---------- Validators ----------
    @validates("timestamp_str")
    def _v_ts(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if not s:
            return None
        return s if _TS_RE.match(s) or len(s.split(":")) == 2 else None

    @validates("content", "platform", "ip_address", "user_agent", "country", "mod_reason", "idempotency_key", "request_id")
    def _trim(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    @validates("position_seconds")
    def _nonneg(self, _k: str, v: float) -> float:
        return max(0.0, float(v or 0.0))

    # ---------- Helpers ----------
    def set_position_from_hhmmss(self, s: str) -> None:
        self.position_seconds = max(0.0, _hhmmss_to_seconds(s))
        self.timestamp_str = s.strip() or None

    def set_hhmmss_from_seconds(self) -> None:
        self.timestamp_str = _seconds_to_hhmmss(self.position_seconds or 0.0)

    def sync_time_fields(self) -> None:
        if self.timestamp_str and not _TS_RE.match(self.timestamp_str) and len(self.timestamp_str.split(":")) != 2:
            self.set_hhmmss_from_seconds()
            return
        if self.timestamp_str and (self.position_seconds == 0.0):
            self.position_seconds = max(0.0, _hhmmss_to_seconds(self.timestamp_str))
        elif not self.timestamp_str:
            self.set_hhmmss_from_seconds()

    def hide(self, reason: Optional[str] = None, *, moderator_user_id: Optional[int] = None) -> None:
        self.is_hidden = True
        self.hidden_at = _utcnow()
        if reason:
            self.mod_reason = reason
        if moderator_user_id:
            self.moderated_by_user_id = moderator_user_id

    def soft_delete(self, reason: Optional[str] = None, *, moderator_user_id: Optional[int] = None) -> None:
        self.is_deleted = True
        self.deleted_at = _utcnow()
        if reason:
            self.mod_reason = reason
        if moderator_user_id:
            self.moderated_by_user_id = moderator_user_id

    def report(self, reason: Optional[str] = None) -> None:
        self.is_reported = True
        if reason:
            self.mod_reason = reason

    # ---- Fingerprint / Dedupe ----
    def compute_fingerprint(self) -> str:
        parts = [
            str(self.video_post_id or ""),
            str(self.action_user_id or ""),
            str(self.event_type or ""),
            str(self.reaction or ""),
            f"{float(self.position_seconds or 0.0):.3f}",
            (self.content or "").strip().lower() if self.event_type == ReplayEventType.comment else "",
        ]
        raw = "|".join(parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def refresh_fingerprint(self) -> None:
        self.fingerprint = self.compute_fingerprint()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ReplayEvent id={self.id} video={self.video_post_id} type={self.event_type} t={self.hhmmss}>"

# --------- Event listeners: normalize & derive fields ---------
@listens_for(ReplayEvent, "before_insert")
def _re_before_insert(_m, _c, target: ReplayEvent) -> None:  # pragma: no cover
    target.position_seconds = max(0.0, float(target.position_seconds or 0.0))
    target.sync_time_fields()
    target.refresh_fingerprint()

@listens_for(ReplayEvent, "before_update")
def _re_before_update(_m, _c, target: ReplayEvent) -> None:  # pragma: no cover
    target.position_seconds = max(0.0, float(target.position_seconds or 0.0))
    target.sync_time_fields()
    target.refresh_fingerprint()
