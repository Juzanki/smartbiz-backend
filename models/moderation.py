# backend/models/moderation.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import os
import hmac
import hashlib
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    from .live_stream import LiveStream
    from .message import Message


# ---------- Enums (portable: native_enum=False) ----------
class ActionType(str, enum.Enum):
    mute    = "mute"
    block   = "block"
    report  = "report"
    ban     = "ban"
    kick    = "kick"
    warn    = "warn"
    timeout = "timeout"

class ActionStatus(str, enum.Enum):
    applied = "applied"
    revoked = "revoked"
    expired = "expired"

class ActionScope(str, enum.Enum):
    room     = "room"      # chumba/stream
    global_  = "global"    # mfumo mzima
    platform = "platform"  # moderation ya nje (yt/tiktok/etc.)

class ReasonCode(str, enum.Enum):
    spam          = "spam"
    abuse         = "abuse"
    hate_speech   = "hate_speech"
    sexual        = "sexual"
    illegal       = "illegal"
    self_harm     = "self_harm"
    scam_fraud    = "scam_fraud"
    impersonation = "impersonation"
    other         = "other"


# ---------- Privacy helpers ----------
def _ip_hash(ip: str) -> str:
    secret = (os.getenv("IP_HASH_SECRET") or "").encode("utf-8")
    data = (ip or "").encode("utf-8")
    return hmac.new(secret, data, hashlib.sha256).hexdigest() if secret else hashlib.sha256(data).hexdigest()

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------- Model ----------
class ModerationAction(Base):
    """
    Kitendo cha moderation kwa mtumiaji (target) na aliyechukua hatua (moderator).
    - Scope: room/global/platform
    - Lifecycle: applied/revoked/expired (+ duration/expires_at)
    - Evidence: message/url/meta
    - Privacy: ip_hash (client_ip hiari kulingana na sera)
    - Appeals: sehemu za rufaa (optional)
    """
    __tablename__ = "moderation_actions"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Wapi ilipotokea
    room_id: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    live_stream_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("live_streams.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # Wahusika
    target_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    moderator_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Anaweza kuwa NULL kwa system/bot auto-moderation",
    )

    # Mahusiano (back_populates lazima yazingatie yale ya User/LiveStream)
    target: Mapped["User"] = relationship(
        "User",
        foreign_keys=lambda: [ModerationAction.target_user_id],
        back_populates="moderations_received",
        passive_deletes=True,
        lazy="selectin",
    )
    moderator: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=lambda: [ModerationAction.moderator_id],
        back_populates="moderations_taken",
        passive_deletes=True,
        lazy="selectin",
    )
    live_stream: Mapped[Optional["LiveStream"]] = relationship(
        "LiveStream",
        foreign_keys=lambda: [ModerationAction.live_stream_id],
        back_populates="moderation_actions",  # LiveStream lazima iwe na moderation_actions
        passive_deletes=True,
        lazy="selectin",
    )

    # Uainishaji
    action: Mapped[ActionType] = mapped_column(
        SQLEnum(ActionType, name="moderation_action_type", native_enum=False, validate_strings=True),
        nullable=False,
        index=True,
    )
    scope: Mapped[ActionScope] = mapped_column(
        SQLEnum(ActionScope, name="moderation_scope", native_enum=False, validate_strings=True),
        default=ActionScope.room,
        nullable=False,
        index=True,
    )
    status: Mapped[ActionStatus] = mapped_column(
        SQLEnum(ActionStatus, name="moderation_status", native_enum=False, validate_strings=True),
        default=ActionStatus.applied,
        nullable=False,
        index=True,
    )

    # Sababu / maelezo
    reason_code: Mapped[ReasonCode] = mapped_column(
        SQLEnum(ReasonCode, name="moderation_reason_code", native_enum=False, validate_strings=True),
        default=ReasonCode.other,
        nullable=False,
        index=True,
    )
    reason: Mapped[Optional[str]] = mapped_column(String(255))
    note: Mapped[Optional[str]] = mapped_column(Text)

    # Muda wa adhabu
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, doc="NULL/0 = bila kikomo")
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Ushahidi
    evidence_message_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    evidence_url:  Mapped[Optional[str]] = mapped_column(String(512))
    evidence_meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    evidence_message: Mapped[Optional["Message"]] = relationship(
        "Message",
        foreign_keys=lambda: [ModerationAction.evidence_message_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # Appeals (hiari)
    appeal_submitted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    appeal_decided_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    appeal_result:       Mapped[Optional[str]] = mapped_column(String(32), doc="upheld|reduced|overturned")
    appeal_note:         Mapped[Optional[str]] = mapped_column(String(255))

    # Audit / dedupe / privacy
    client_ip: Mapped[Optional[str]] = mapped_column(String(64))
    ip_hash:   Mapped[Optional[str]] = mapped_column(String(128), index=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(400))
    request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
        index=True,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # ---------- Indices & Guards ----------
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_modact_idem"),
        Index("ix_modact_target_time", "target_user_id", "created_at"),
        Index("ix_modact_room_time", "room_id", "created_at"),
        Index("ix_modact_stream_time", "live_stream_id", "created_at"),
        Index("ix_modact_status_time", "status", "created_at"),
        Index("ix_modact_action_scope", "action", "scope"),
        Index("ix_modact_moderator_time", "moderator_id", "created_at"),
        Index("ix_modact_target_active", "target_user_id", "status", "expires_at"),
        CheckConstraint("target_user_id IS NOT NULL", name="ck_modact_target_required"),
        CheckConstraint(
            "(scope <> 'room') OR (room_id IS NOT NULL OR live_stream_id IS NOT NULL)",
            name="ck_modact_room_scope_target",
        ),
        CheckConstraint("(duration_seconds IS NULL) OR (duration_seconds >= 0)", name="ck_modact_duration_nonneg"),
        CheckConstraint("(status <> 'revoked') OR (revoked_at IS NOT NULL)", name="ck_modact_revoked_ts"),
        {"extend_existing": True},
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def is_active(self) -> bool:
        if self.status != ActionStatus.applied:
            return False
        if self.expires_at and _utcnow() >= self.expires_at:
            return False
        return True

    @is_active.expression
    def is_active(cls):
        return (cls.status == ActionStatus.applied) & (
            (cls.expires_at.is_(None)) | (cls.expires_at > func.now())
        )

    @hybrid_property
    def remaining_seconds(self) -> Optional[int]:
        if not self.expires_at:
            return None
        return max(0, int((self.expires_at - _utcnow()).total_seconds()))

    @remaining_seconds.expression
    def remaining_seconds(cls):
        return func.cast(func.greatest(0, func.extract("epoch", cls.expires_at - func.now())), Integer)

    # ---------- Helpers ----------
    def apply_duration(self, seconds: int | None) -> None:
        self.duration_seconds = int(seconds) if seconds is not None else None
        if self.duration_seconds and self.duration_seconds > 0:
            self.expires_at = _utcnow() + dt.timedelta(seconds=self.duration_seconds)
        else:
            self.expires_at = None

    def extend(self, *, seconds: int) -> None:
        add = max(1, int(seconds))
        base = self.expires_at if self.expires_at and self.expires_at > _utcnow() else _utcnow()
        self.expires_at = base + dt.timedelta(seconds=add)
        self.duration_seconds = (self.duration_seconds or 0) + add

    def lift(self, *, reason: str | None = None) -> None:
        self.status = ActionStatus.revoked
        self.revoked_at = _utcnow()
        if reason:
            self.note = (self.note + " | " if self.note else "") + f"revoked: {reason.strip()}"

    def mark_expired_if_needed(self) -> bool:
        if self.status == ActionStatus.applied and self.expires_at and _utcnow() >= self.expires_at:
            self.status = ActionStatus.expired
            return True
        return False

    def set_ip(self, ip: Optional[str]) -> None:
        if ip:
            self.ip_hash = _ip_hash(ip)
            if os.getenv("STORE_PLAIN_IP", "0").lower() in {"1", "true", "yes", "on"}:
                self.client_ip = ip
            else:
                self.client_ip = None
        else:
            self.ip_hash = None
            self.client_ip = None

    def __repr__(self) -> str:  # pragma: no cover
        scope = self.live_stream_id or self.room_id
        return f"<ModerationAction id={self.id} action={self.action} target={self.target_user_id} scope={scope} status={self.status}>"


# ---------- Validators / Normalizers ----------
@validates("reason", "note", "evidence_url", "request_id", "idempotency_key", "appeal_result", "appeal_note", "room_id")
def _trim_texts(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    return v or None

@validates("duration_seconds")
def _nonneg_duration(_inst, _key, value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    v = int(value)
    if v < 0:
        raise ValueError("duration_seconds must be >= 0")
    return v


# ---------- Event hooks ----------
from sqlalchemy.event import listens_for

@listens_for(ModerationAction, "before_insert")
def _mod_before_insert(_m, _c, t: ModerationAction) -> None:
    if (t.duration_seconds or 0) > 0 and not t.expires_at:
        t.expires_at = _utcnow() + dt.timedelta(seconds=int(t.duration_seconds))
    if t.client_ip and not t.ip_hash:
        t.ip_hash = _ip_hash(t.client_ip)
        if os.getenv("STORE_PLAIN_IP", "0").lower() not in {"1", "true", "yes", "on"}:
            t.client_ip = None
    if t.appeal_result:
        t.appeal_result = t.appeal_result.strip() or None
    if t.appeal_note:
        t.appeal_note = t.appeal_note.strip() or None

@listens_for(ModerationAction, "before_update")
def _mod_before_update(_m, _c, t: ModerationAction) -> None:
    if (t.duration_seconds or 0) > 0 and not t.expires_at and t.status == ActionStatus.applied:
        t.expires_at = _utcnow() + dt.timedelta(seconds=int(t.duration_seconds))
    if t.status == ActionStatus.applied and t.expires_at and _utcnow() >= t.expires_at:
        t.status = ActionStatus.expired
    if t.client_ip and not t.ip_hash:
        t.ip_hash = _ip_hash(t.client_ip)
        if os.getenv("STORE_PLAIN_IP", "0").lower() not in {"1", "true", "yes", "on"}):
            t.client_ip = None
