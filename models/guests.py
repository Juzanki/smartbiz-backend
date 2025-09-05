# backend/models/guest.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, Dict, Any, TYPE_CHECKING, Iterable

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method
from sqlalchemy.event import listens_for
from sqlalchemy.ext.mutable import MutableDict, MutableList

from backend.db import Base
from backend.models._types import JSON_VARIANT  # DECIMAL_TYPE si hitaji hapa

if TYPE_CHECKING:
    from .user import User
    from .live_stream import LiveStream


# ───────── Enums ─────────
class GuestRole(str, enum.Enum):
    host       = "host"
    cohost     = "cohost"
    moderator  = "moderator"
    speaker    = "speaker"   # jukwaani (audio/video)
    guest      = "guest"     # mtazamaji wa kawaida

class GuestStatus(str, enum.Enum):
    pending       = "pending"
    approved      = "approved"
    denied        = "denied"
    kicked        = "kicked"
    banned        = "banned"
    left          = "left"
    disconnected  = "disconnected"

class StageStatus(str, enum.Enum):
    offstage   = "offstage"
    requested  = "requested"   # ameomba kuongea
    onstage    = "onstage"     # anazungumza/anaonekana

class GuestAction(str, enum.Enum):
    send_message   = "send_message"
    send_reaction  = "send_reaction"
    send_gift      = "send_gift"
    raise_hand     = "raise_hand"
    request_stage  = "request_stage"
    vote           = "vote"           # polls
    ask_question   = "ask_question"   # Q&A
    share_screen   = "share_screen"
    report_user    = "report_user"
    invite_friend  = "invite_friend"


class Guest(Base):
    """
    Ushiriki wa mtumiaji kwenye live stream (roles, status, capabilities, metrics).
    • JSON mutable: permissions, preferences, badges
    • Stage flow: request -> approve -> onstage
    • Cooldowns/rate-limits + takwimu za matumizi
    """
    __tablename__ = "guests"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("room_id", "user_id", name="uq_guest_room_user"),
        Index("ix_guest_user_joined", "user_id", "joined_at"),
        Index("ix_guest_room_status", "room_id", "status"),
        Index("ix_guest_stream_role", "live_stream_id", "role"),
        Index("ix_guest_status_joined", "status", "joined_at"),
        Index("ix_guest_stage_state", "live_stream_id", "stage_status"),
        CheckConstraint("length(trim(room_id)) >= 2", name="ck_guest_room_len"),
        CheckConstraint("(NOT is_host) OR (role = 'host')", name="ck_guest_is_host_matches_role"),
        CheckConstraint("messages_sent >= 0 AND reactions_sent >= 0 AND gifts_sent >= 0", name="ck_guest_counts_nonneg"),
        CheckConstraint("time_speaking_seconds >= 0 AND time_watching_seconds >= 0", name="ck_guest_times_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Utambulisho wa chumba/stream
    room_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    live_stream_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("live_streams.id", ondelete="SET NULL"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Nafasi na hali
    role: Mapped[GuestRole] = mapped_column(
        SQLEnum(GuestRole, name="guest_role", native_enum=False, validate_strings=True),
        default=GuestRole.guest,
        nullable=False,
        index=True,
    )
    status: Mapped[GuestStatus] = mapped_column(
        SQLEnum(GuestStatus, name="guest_status", native_enum=False, validate_strings=True),
        default=GuestStatus.pending,
        nullable=False,
        index=True,
    )
    stage_status: Mapped[StageStatus] = mapped_column(
        SQLEnum(StageStatus, name="guest_stage_status", native_enum=False, validate_strings=True),
        default=StageStatus.offstage,
        nullable=False,
        index=True,
    )

    # Back-compat flags
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_host:     Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    approved_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # Nyakati
    join_requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    approved_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    denied_at:      Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    joined_at:      Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    left_at:        Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    kicked_at:      Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    banned_at:      Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    ban_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    last_seen_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Mute/Video/Hands + stage windows
    is_muted:        Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_camera_off:   Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    hand_raised:     Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stage_entered_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    stage_left_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Cooldowns / rate-limits
    slowmode_until: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    mute_until:     Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    can_gift:       Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_chat:       Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_react:      Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Takwimu (metrics)
    messages_sent:        Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    reactions_sent:       Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    gifts_sent:           Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    questions_asked:      Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    votes_cast:           Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    reports_made:         Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    time_speaking_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    time_watching_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Wasifu wa muda (kwa wageni/anon)
    nickname:    Mapped[Optional[str]] = mapped_column(String(80))
    avatar_url:  Mapped[Optional[str]] = mapped_column(String(512))
    badges:      Mapped[Optional[list]] = mapped_column(MutableList.as_mutable(JSON_VARIANT))  # ["early_bird","vip"]

    # Uwezo/Mapendeleo (mutable JSON)
    # permissions mfano:
    #   {"send_message": true, "share_screen": false, "ask_question": true}
    permissions:  Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))
    preferences:  Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))  # {"lang":"sw","theme":"dark"}

    # Mazingira ya mteja
    device:     Mapped[Optional[str]] = mapped_column(String(80))
    user_agent: Mapped[Optional[str]] = mapped_column(String(400))
    client_ip:  Mapped[Optional[str]] = mapped_column(String(64))
    request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    network_quality: Mapped[Optional[int]] = mapped_column(Integer)  # 0..5; kusaidia auto-downgrade

    # Audit
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ───────── Relationships ─────────
    user: Mapped["User"] = relationship(
        "User",
        back_populates="guest_entries",
        foreign_keys=lambda: [Guest.user_id],
        passive_deletes=True,
        lazy="selectin",
    )
    approved_by: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="guest_approvals",
        foreign_keys=lambda: [Guest.approved_by_user_id],
        passive_deletes=True,
        lazy="selectin",
    )
    live_stream: Mapped[Optional["LiveStream"]] = relationship(
        "LiveStream",
        back_populates="guests",
        foreign_keys=lambda: [Guest.live_stream_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # ───────── Validators / Hybrids ─────────
    @validates("room_id")
    def _v_room_id(self, _k: str, v: Optional[str]) -> Optional[str]:
        return (v or "").strip() or None

    @validates("network_quality")
    def _v_quality(self, _k: str, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        iv = int(v)
        if not (0 <= iv <= 5):
            raise ValueError("network_quality must be between 0 and 5")
        return iv

    @hybrid_property
    def is_active(self) -> bool:
        if self.status in (GuestStatus.kicked, GuestStatus.banned, GuestStatus.left, GuestStatus.denied):
            return False
        if self.joined_at is None:
            return False
        if self.ban_expires_at and dt.datetime.now(dt.timezone.utc) >= self.ban_expires_at:
            return self.status not in (GuestStatus.kicked, GuestStatus.denied)
        return True

    @hybrid_property
    def is_banned_now(self) -> bool:
        if self.status != GuestStatus.banned:
            return False
        if self.ban_expires_at is None:
            return True
        return dt.datetime.now(dt.timezone.utc) < self.ban_expires_at

    @hybrid_property
    def slowmode_active(self) -> bool:
        return bool(self.slowmode_until and dt.datetime.now(dt.timezone.utc) < self.slowmode_until)

    @hybrid_property
    def muted_now(self) -> bool:
        if self.mute_until and dt.datetime.now(dt.timezone.utc) < self.mute_until:
            return True
        return self.is_muted

    # ───────── Permission helpers ─────────
    def grant(self, *actions: GuestAction | str) -> None:
        perms = dict(self.permissions or {})
        for a in actions:
            key = a.value if isinstance(a, GuestAction) else str(a)
            perms[key] = True
        self.permissions = perms

    def revoke(self, *actions: GuestAction | str) -> None:
        perms = dict(self.permissions or {})
        for a in actions:
            key = a.value if isinstance(a, GuestAction) else str(a)
            perms[key] = False
        self.permissions = perms

    @hybrid_method
    def can(self, action: GuestAction | str) -> bool:
        key = action.value if isinstance(action, GuestAction) else str(action)
        # fallback kwenye flags za juu (can_chat/can_gift/can_react)
        if key == GuestAction.send_message.value:
            return bool((self.permissions or {}).get(key, self.can_chat)) and not self.slowmode_active
        if key == GuestAction.send_reaction.value:
            return bool((self.permissions or {}).get(key, self.can_react))
        if key == GuestAction.send_gift.value:
            return bool((self.permissions or {}).get(key, self.can_gift))
        return bool((self.permissions or {}).get(key, True))

    # ───────── Stage flow ─────────
    def request_stage(self) -> None:
        self.stage_status = StageStatus.requested
        self.hand_raised = True

    def approve_to_stage(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        self.stage_status = StageStatus.onstage
        self.hand_raised = False
        self.stage_entered_at = now
        if self.status == GuestStatus.pending:
            self.status = GuestStatus.approved
            self.is_approved = True
            self.joined_at = self.joined_at or now

    def leave_stage(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        self.stage_status = StageStatus.offstage
        self.stage_left_at = now
        # ongeza muda aliozungumza
        if self.stage_entered_at:
            dt_seconds = int((now - self.stage_entered_at).total_seconds())
            self.time_speaking_seconds = (self.time_speaking_seconds or 0) + max(0, dt_seconds)
        self.stage_entered_at = None

    # ───────── Actions + counters ─────────
    def record_message(self) -> None:
        self.messages_sent = (self.messages_sent or 0) + 1
        self.last_seen_at = dt.datetime.now(dt.timezone.utc)

    def record_reaction(self) -> None:
        self.reactions_sent = (self.reactions_sent or 0) + 1
        self.last_seen_at = dt.datetime.now(dt.timezone.utc)

    def record_gift(self) -> None:
        self.gifts_sent = (self.gifts_sent or 0) + 1
        self.last_seen_at = dt.datetime.now(dt.timezone.utc)

    def record_vote(self) -> None:
        self.votes_cast = (self.votes_cast or 0) + 1
        self.last_seen_at = dt.datetime.now(dt.timezone.utc)

    def record_question(self) -> None:
        self.questions_asked = (self.questions_asked or 0) + 1
        self.last_seen_at = dt.datetime.now(dt.timezone.utc)

    def record_watchtime(self, seconds: int) -> None:
        self.time_watching_seconds = (self.time_watching_seconds or 0) + max(0, int(seconds))

    # ───────── Lifecycle helpers ─────────
    def approve(self, by_user_id: int | None = None) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        self.status = GuestStatus.approved
        self.is_approved = True
        self.approved_at = now
        self.approved_by_user_id = by_user_id
        self.joined_at = self.joined_at or now

    def deny(self) -> None:
        self.status = GuestStatus.denied
        self.is_approved = False
        self.denied_at = dt.datetime.now(dt.timezone.utc)

    def kick(self) -> None:
        self.status = GuestStatus.kicked
        now = dt.datetime.now(dt.timezone.utc)
        self.kicked_at = now
        self.left_at = self.left_at or now
        self.leave_stage()

    def ban(self, *, until: dt.datetime | None = None) -> None:
        self.status = GuestStatus.banned
        now = dt.datetime.now(dt.timezone.utc)
        self.banned_at = now
        self.ban_expires_at = until
        self.left_at = self.left_at or now
        self.leave_stage()

    def unban(self) -> None:
        self.status = GuestStatus.pending
        self.ban_expires_at = None

    def mark_joined(self) -> None:
        self.joined_at = dt.datetime.now(dt.timezone.utc)
        if self.status == GuestStatus.pending:
            self.status = GuestStatus.approved
            self.is_approved = True

    def mark_left(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        self.left_at = now
        if self.status not in (GuestStatus.kicked, GuestStatus.banned):
            self.status = GuestStatus.left
        self.leave_stage()

    def mute(self, *, until: dt.datetime | None = None) -> None:
        self.is_muted = True
        self.mute_until = until

    def unmute(self) -> None:
        self.is_muted = False
        self.mute_until = None

    def raise_hand(self) -> None:  self.hand_raised = True
    def lower_hand(self) -> None:  self.hand_raised = False

    def set_slowmode(self, seconds: int | None) -> None:
        if seconds is None or seconds <= 0:
            self.slowmode_until = None
        else:
            self.slowmode_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(seconds))

    def set_badges(self, items: Iterable[str]) -> None:
        self.badges = sorted({str(b).strip() for b in (items or []) if b})

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Guest id={self.id} room={self.room_id} user={self.user_id} role={self.role} status={self.status} stage={self.stage_status}>"

# ───────── Normalization hooks ─────────
@listens_for(Guest, "before_insert")
def _guest_before_insert(_m, _c, t: Guest) -> None:  # pragma: no cover
    if t.room_id:
        t.room_id = t.room_id.strip()
    if t.permissions is None:
        # ruhusu vitendo vya msingi kwa mgeni
        t.permissions = {"send_message": True, "send_reaction": True, "ask_question": True, "vote": True}

@listens_for(Guest, "before_update")
def _guest_before_update(_m, _c, t: Guest) -> None:  # pragma: no cover
    if t.room_id:
        t.room_id = t.room_id.strip()
