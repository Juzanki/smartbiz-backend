# backend/models/superchat.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from typing import Optional, Dict, Any, List, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

if TYPE_CHECKING:
    from .user import User

# --------------------------------- Enums/choices ---------------------------------

_ALLOWED_STATUS = (
    "pending", "processing", "paid", "refunded", "failed", "canceled", "hidden", "flagged"
)
_ALLOWED_VISIBILITY = ("public", "followers", "private")
_ALLOWED_PLATFORM = ("app", "web", "android", "ios", "other")


class Superchat(Base):
    """
    Superchat — paid highlighted messages for live rooms (mobile-first, scalable).

    - Amount in *minor units* (Integer) for safe money handling.
    - Strong constraints + hot indexes for feeds/leaderboards.
    - Lifecycle helpers (processing/paid/refunded/etc).
    - Highlight window (start/end) + pin & visibility controls.
    - PSP idempotency/external refs for dedupe.
    - Portable JSON attachments & metadata.
    """
    __tablename__ = "superchats"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_superchat_idempotency"),
        Index("ix_superchats_room_created", "room_id", "created_at"),
        Index("ix_superchats_user_created", "user_id", "created_at"),
        Index("ix_superchats_status_created", "status", "created_at"),
        Index("ix_superchats_room_amount", "room_id", "amount"),
        Index("ix_superchats_day", "day_bucket"),
        CheckConstraint("amount > 0", name="ck_superchat_amount_positive"),
        CheckConstraint(
            "status in ('pending','processing','paid','refunded','failed','canceled','hidden','flagged')",
            name="ck_superchat_status_enum",
        ),
        CheckConstraint(
            "visibility in ('public','followers','private')",
            name="ck_superchat_visibility_enum",
        ),
        CheckConstraint(
            "platform in ('app','web','android','ios','other')",
            name="ck_superchat_platform_enum",
        ),
        CheckConstraint("pinned_seconds >= 0", name="ck_superchat_pinned_seconds_nonneg"),
        CheckConstraint("length(message) > 0", name="ck_superchat_message_nonempty"),
    )

    # Identity
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    room_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)

    # Who
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user: Mapped[Optional["User"]] = relationship("User", lazy="selectin")

    # Content & money (amount is minor units / coins)
    message: Mapped[str] = mapped_column(String(240), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False, doc="Minor units / coins")
    currency: Mapped[Optional[str]] = mapped_column(String(12), default="TZS")

    # Visuals / highlight
    highlight_color: Mapped[Optional[str]] = mapped_column(String(16), default="#FFD700")  # gold
    pinned_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    highlight_start_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    highlight_end_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Channel / moderation
    platform: Mapped[str] = mapped_column(String(20), default="app", nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), default="public", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Payment / PSP
    payment_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    tx_ref: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    provider: Mapped[Optional[str]] = mapped_column(String(24))

    # Device / context
    device_id: Mapped[Optional[str]] = mapped_column(String(80))
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(String(300))

    # Attachments & extra metadata (portable JSON with change tracking)
    attachments: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Analytics
    day_bucket: Mapped[dt.date] = mapped_column(Date, server_default=func.current_date(), nullable=False, index=True)

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    paid_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # ----------------------------- Helpers (no DB I/O) -----------------------------

    def set_highlight(self, *, seconds: int, start: Optional[dt.datetime] = None) -> None:
        if seconds < 0:
            raise ValueError("seconds must be >= 0")
        self.pinned_seconds = seconds
        start_at = start or dt.datetime.now(dt.timezone.utc)
        self.highlight_start_at = start_at if seconds > 0 else None
        self.highlight_end_at = (start_at + dt.timedelta(seconds=seconds)) if seconds > 0 else None

    def pin(self) -> None:
        self.is_pinned = True

    def unpin(self) -> None:
        self.is_pinned = False

    def hide(self) -> None:
        self.status = "hidden"

    def unhide(self) -> None:
        if self.status == "hidden":
            self.status = "paid"

    def soft_delete(self) -> None:
        self.is_deleted = True

    def set_idempotency(self, key: Optional[str]) -> None:
        self.idempotency_key = key[:64] if key else None

    # ---- Lifecycle
    def mark_processing(
        self,
        *,
        provider: Optional[str] = None,
        payment_id: Optional[str] = None,
        tx_ref: Optional[str] = None,
    ) -> None:
        self.status = "processing"
        if provider:
            self.provider = provider[:24]
        if payment_id:
            self.payment_id = payment_id[:80]
        if tx_ref:
            self.tx_ref = tx_ref[:80]

    def mark_paid(self, when: Optional[dt.datetime] = None) -> None:
        self.status = "paid"
        self.paid_at = when or dt.datetime.now(dt.timezone.utc)

    def mark_refunded(self, when: Optional[dt.datetime] = None) -> None:
        self.status = "refunded"
        self.refunded_at = when or dt.datetime.now(dt.timezone.utc)

    def mark_failed(self) -> None:
        self.status = "failed"

    def cancel(self) -> None:
        if self.status not in ("pending", "processing"):
            raise ValueError("Only pending/processing superchats can be canceled")
        self.status = "canceled"

    # ---- Attachments / meta
    def add_attachment(self, url: str, name: Optional[str] = None, size: Optional[int] = None, kind: Optional[str] = None) -> None:
        files = list(self.attachments or [])
        files.append({"url": url, "name": name, "size": size, "kind": kind})
        self.attachments = files

    def put_meta(self, key: str, value: Any) -> None:
        extra = dict(self.meta or {})
        extra[key] = value
        self.meta = extra

    # ---- API projection (mobile friendly)
    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "room_id": self.room_id,
            "user_id": self.user_id,
            "message": self.message,
            "amount_minor": self.amount,
            "currency": self.currency,
            "highlight_color": self.highlight_color,
            "pinned_seconds": self.pinned_seconds,
            "highlight_start_at": self.highlight_start_at.isoformat() if self.highlight_start_at else None,
            "highlight_end_at": self.highlight_end_at.isoformat() if self.highlight_end_at else None,
            "is_pinned": self.is_pinned,
            "platform": self.platform,
            "visibility": self.visibility,
            "status": self.status,
            "attachments": self.attachments or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Superchat id={self.id} room={self.room_id} user={self.user_id} "
            f"amount_minor={self.amount} {self.currency} status={self.status}>"
        )
