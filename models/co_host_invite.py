# backend/models/co_host_invite.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import secrets
import datetime as dt
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    from .live_stream import LiveStream  # __tablename__ = "live_streams"


# --------- Enums ---------
class InviteStatus(str, enum.Enum):
    pending  = "pending"
    sent     = "sent"
    accepted = "accepted"
    declined = "declined"
    canceled = "canceled"
    expired  = "expired"


class InviteChannel(str, enum.Enum):
    inapp = "inapp"
    email = "email"
    sms   = "sms"
    link  = "link"
    other = "other"


# --------- Model ---------
class CoHostInvite(Base):
    """
    Mwaliko wa kumualika mtumiaji kuwa co-host kwenye LiveStream fulani.
    Huweka token, muda wa kuisha, na hali (status) ya mwaliko.
    """
    __tablename__ = "co_host_invites"
    __mapper_args__ = {"eager_defaults": True}

    __table_args__ = (
        # hakikisha mwaliko wa mtu huyu kwenye stream hii katika status ile ile haujirudii
        UniqueConstraint("live_stream_id", "invitee_id", "status",
                         name="uq_invite_stream_invitee_status"),
        # host != invitee
        CheckConstraint("host_id <> invitee_id", name="ck_invite_distinct_users"),
        # token lazima iwe ndefu au NULL
        CheckConstraint("token IS NULL OR length(trim(token)) >= 16",
                        name="ck_invite_token_len"),
        # indexi za kawaida
        Index("ix_invite_stream_status", "live_stream_id", "status"),
        Index("ix_invite_host_sent", "host_id", "sent_at"),
        Index("ix_invite_invitee_sent", "invitee_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Nani / wapi
    live_stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    host_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invitee_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Hali & channel (portable enums)
    status: Mapped[InviteStatus] = mapped_column(
        SQLEnum(InviteStatus, name="cohost_invite_status",
                native_enum=False, validate_strings=True),
        default=InviteStatus.pending, nullable=False, index=True,
    )
    channel: Mapped[InviteChannel] = mapped_column(
        SQLEnum(InviteChannel, name="cohost_invite_channel",
                native_enum=False, validate_strings=True),
        default=InviteChannel.inapp, nullable=False, index=True,
    )

    # Token / ujumbe / metadata
    token: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    message: Mapped[Optional[str]] = mapped_column(String(280))
    # portable JSON (PG: JSONB)
    meta: Mapped[Optional[dict]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Muda
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    declined_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Mahusiano
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream",
        back_populates="cohost_invites",  # hakikisha LiveStream ina attribute hii
        foreign_keys=[live_stream_id],
        passive_deletes=True,
        lazy="selectin",
    )
    host: Mapped["User"] = relationship(
        "User", foreign_keys=[host_id], passive_deletes=True, lazy="selectin"
    )
    invitee: Mapped["User"] = relationship(
        "User", foreign_keys=[invitee_id], passive_deletes=True, lazy="selectin"
    )

    # --------- Helpers ---------
    @staticmethod
    def _now() -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    @staticmethod
    def generate_token(length: int = 48) -> str:
        return secrets.token_urlsafe(length)[:length]

    @validates("token")
    def _clean_token(self, _k, v: Optional[str]) -> Optional[str]:
        return (v or "").strip() or None

    def issue(self, ttl_minutes: int = 120) -> str:
        """Weka token + expiry na kuhamisha status kwenda 'sent'."""
        t = self.generate_token()
        self.token = t
        self.expires_at = self._now() + dt.timedelta(minutes=ttl_minutes)
        self.status = InviteStatus.sent
        self.sent_at = self._now()
        return t

    def _ensure_active(self) -> None:
        if self.status in (InviteStatus.canceled, InviteStatus.expired):
            raise ValueError("Invite is no longer active.")
        if self.expires_at and self._now() >= self.expires_at:
            self.status = InviteStatus.expired
            raise ValueError("Invite has expired.")

    def is_token_valid(self, token: str) -> bool:
        if not self.token or token != self.token:
            return False
        if self.expires_at and self._now() >= self.expires_at:
            return False
        return self.status in (InviteStatus.pending, InviteStatus.sent)

    def accept(self, *, token: Optional[str] = None) -> None:
        if token is not None and not self.is_token_valid(token):
            raise ValueError("Invalid or expired token.")
        self._ensure_active()
        self.status = InviteStatus.accepted
        self.accepted_at = self._now()

    def decline(self) -> None:
        self._ensure_active()
        self.status = InviteStatus.declined
        self.declined_at = self._now()

    def cancel(self) -> None:
        if self.status in (InviteStatus.accepted,):
            raise ValueError("Cannot cancel an already accepted invite.")
        self.status = InviteStatus.canceled
        self.canceled_at = self._now()

    def mark_expired(self) -> None:
        self.status = InviteStatus.expired

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<CoHostInvite id={self.id} stream={self.live_stream_id} host={self.host_id} "
            f"invitee={self.invitee_id} status={self.status}>"
        )


# --------- Normalizers / Guards ---------
@listens_for(CoHostInvite, "before_insert")
def _invite_before_insert(_m, _c, inv: CoHostInvite) -> None:
    if inv.token:
        inv.token = inv.token.strip()[:64] or None
    if inv.message:
        inv.message = inv.message.strip()[:280]
    # ensure chronology is sensible
    if inv.sent_at is None and inv.status in (InviteStatus.sent,):
        inv.sent_at = func.now()


@listens_for(CoHostInvite, "before_update")
def _invite_before_update(_m, _c, inv: CoHostInvite) -> None:
    if inv.token:
        inv.token = inv.token.strip()[:64] or None
    if inv.message:
        inv.message = inv.message.strip()[:280]
