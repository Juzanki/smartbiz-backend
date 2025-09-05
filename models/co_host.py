# backend/models/co_host.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import secrets
import datetime as dt
from typing import Optional, Dict, Any, TYPE_CHECKING

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
    from .live_stream import LiveStream


# --------- Enums ---------
class CoHostStatus(str, enum.Enum):
    pending  = "pending"
    invited  = "invited"
    accepted = "accepted"
    declined = "declined"
    removed  = "removed"
    blocked  = "blocked"


class CoHostRole(str, enum.Enum):
    cohost    = "cohost"
    moderator = "moderator"
    guest     = "guest"


# --------- Model ---------
class CoHost(Base):
    """
    Ushiriki wa mtumiaji kama co-host/mod/guest kwenye stream fulani.

    Notes
    - Unique (stream_id, cohost_user_id)
    - host/cohost lazima wawe watu wawili tofauti
    - `permissions` ni JSON portable (PG: JSONB)
    - Invite token + expiry kwa mwaliko wa haraka
    """
    __tablename__ = "co_hosts"
    __mapper_args__ = {"eager_defaults": True}

    __table_args__ = (
        UniqueConstraint("stream_id", "cohost_user_id", name="uq_cohost_stream_user"),
        Index("ix_cohost_stream_status", "stream_id", "status"),
        Index("ix_cohost_user_created", "cohost_user_id", "created_at"),
        Index("ix_cohost_host_stream", "host_user_id", "stream_id"),
        # Guards
        CheckConstraint("host_user_id <> cohost_user_id", name="ck_cohost_distinct_users"),
        CheckConstraint(
            "invite_token IS NULL OR length(trim(invite_token)) >= 16",
            name="ck_cohost_invite_token_len",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Where
    stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Who
    host_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cohost_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Role / status
    role: Mapped[CoHostRole] = mapped_column(
        SQLEnum(CoHostRole, name="cohost_role", native_enum=False, validate_strings=True),
        default=CoHostRole.cohost,
        nullable=False,
        index=True,
    )
    status: Mapped[CoHostStatus] = mapped_column(
        SQLEnum(CoHostStatus, name="cohost_status", native_enum=False, validate_strings=True),
        default=CoHostStatus.pending,
        nullable=False,
        index=True,
    )

    # Invitation
    invited_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )
    invite_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    invite_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Extra perms / metadata — portable JSON + mutable
    permissions: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        as_mutable_json(JSON_VARIANT),
        default=None,
    )

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    accepted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    declined_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    removed_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    joined_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    left_at:     Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Relationships
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream",
        back_populates="co_hosts",
        foreign_keys=[stream_id],
        passive_deletes=True,
        lazy="selectin",
    )
    host: Mapped["User"] = relationship(
        "User",
        back_populates="co_host_as_host",
        foreign_keys=[host_user_id],
        passive_deletes=True,
        lazy="selectin",
    )
    cohost: Mapped["User"] = relationship(
        "User",
        back_populates="co_host_as_cohost",
        foreign_keys=[cohost_user_id],
        passive_deletes=True,
        lazy="selectin",
    )
    invited_by: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[invited_by_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # --------- Validators / helpers ---------
    @validates("invite_token")
    def _normalize_invite(self, _key: str, value: Optional[str]) -> Optional[str]:
        v = (value or "").strip()
        return v if v else None

    def generate_invite(self, ttl_minutes: int = 60) -> str:
        token = secrets.token_urlsafe(32)
        self.invite_token = token
        self.invite_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=ttl_minutes)
        self.status = CoHostStatus.invited
        return token

    def accept(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        self.status = CoHostStatus.accepted
        self.accepted_at = now
        if not self.joined_at:
            self.joined_at = now

    def decline(self) -> None:
        self.status = CoHostStatus.declined
        self.declined_at = dt.datetime.now(dt.timezone.utc)

    def remove(self) -> None:
        self.status = CoHostStatus.removed
        self.removed_at = dt.datetime.now(dt.timezone.utc)

    def block(self) -> None:
        self.status = CoHostStatus.blocked
        # unaweza pia kuweka reason kwenye permissions e.g., {"blocked_reason": "..."}.

    def join(self) -> None:
        self.joined_at = dt.datetime.now(dt.timezone.utc)

    def leave(self) -> None:
        self.left_at = dt.datetime.now(dt.timezone.utc)

    def is_invite_valid(self, token: str) -> bool:
        if not self.invite_token or token != self.invite_token:
            return False
        if self.invite_expires_at and dt.datetime.now(dt.timezone.utc) > self.invite_expires_at:
            return False
        return self.status in (CoHostStatus.invited, CoHostStatus.pending)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<CoHost id={self.id} stream={self.stream_id} host={self.host_user_id} "
            f"cohost={self.cohost_user_id} role={self.role} status={self.status}>"
        )


# --------- Normalizers ---------
@listens_for(CoHost, "before_insert")
def _cohost_before_insert(_m, _c, ch: CoHost) -> None:
    # sanitize token len & distinct users (extra safety beyond constraints)
    if ch.host_user_id == ch.cohost_user_id:
        raise ValueError("host_user_id and cohost_user_id must be different")
    if ch.invite_token:
        ch.invite_token = ch.invite_token.strip()[:64] or None


@listens_for(CoHost, "before_update")
def _cohost_before_update(_m, _c, ch: CoHost) -> None:
    if ch.invite_token:
        ch.invite_token = ch.invite_token.strip()[:64] or None
