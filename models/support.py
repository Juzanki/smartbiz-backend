# backend/models/support.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from typing import Optional, List, Dict, Any, Iterable

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base

# ----------------------------- Enums/choices -----------------------------

_ALLOWED_STATUS   = ("open", "in_progress", "waiting_user", "waiting_agent", "resolved", "closed")
_ALLOWED_PRIORITY = ("low", "normal", "high", "urgent")
_ALLOWED_CATEGORY = ("bug", "payment", "feedback", "account", "abuse", "other")
_ALLOWED_CHANNEL  = ("app", "web", "email", "whatsapp", "telegram", "sms", "other")

# ----------------------------- Support Ticket -----------------------------

class SupportTicket(Base):
    """
    SupportTicket — scalable, mobile-first support workflow.

    Key features:
    - SQLAlchemy 2.0 typed mappings
    - Status/priority/category constraints
    - Assignment + SLA timestamps
    - CSAT capture + tags + channel
    - Threaded messages (SupportMessage)
    """
    __tablename__ = "support_tickets"
    __table_args__ = (
        UniqueConstraint("ticket_key", name="uq_support_ticket_key"),
        CheckConstraint(f"status IN { _ALLOWED_STATUS }",   name="ck_support_status_allowed"),
        CheckConstraint(f"priority IN { _ALLOWED_PRIORITY }", name="ck_support_priority_allowed"),
        CheckConstraint(f"category IN { _ALLOWED_CATEGORY }", name="ck_support_category_allowed"),
        CheckConstraint(f"channel IN { _ALLOWED_CHANNEL }",   name="ck_support_channel_allowed"),
        CheckConstraint("length(subject) > 0",     name="ck_support_subject_nonempty"),
        CheckConstraint("length(description) > 0", name="ck_support_desc_nonempty"),
        CheckConstraint(
            "csat_score IS NULL OR (csat_score BETWEEN 1 AND 5)",
            name="ck_support_csat_bounds",
        ),
        Index("ix_support_user_status", "user_id", "status"),
        Index("ix_support_assignee_status", "assigned_to", "status"),
        Index("ix_support_created", "created_at"),
        Index("ix_support_priority_created", "priority", "created_at"),
        Index("ix_support_category_created", "category", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Ownership
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # NB: jina halisi la FK ni `assigned_to` (sio assigned_to_user_id)
    assigned_to: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )

    # Identity & content
    ticket_key: Mapped[Optional[str]] = mapped_column(
        String(24), default=None, index=True,
        doc="Public ticket ref (e.g., TKT-2025-0012)",
    )
    subject: Mapped[str]   = mapped_column(String(150), nullable=False)
    category: Mapped[str]  = mapped_column(String(24), default="other",  nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Attachments & tags (portable JSON)
    attachments: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(
        JSON, default=None, doc='[{"url": "...", "name": "...", "size": 12345}]'
    )
    tags: Mapped[List[str]] = mapped_column(JSON, default=list, nullable=False)

    # Channel & locale
    channel: Mapped[str]       = mapped_column(String(16), default="app", nullable=False)
    language: Mapped[Optional[str]] = mapped_column(String(12), default="en", doc="IETF/ISO code")

    # Workflow
    status: Mapped[str]   = mapped_column(String(16), default="open",   nullable=False)
    priority: Mapped[str] = mapped_column(String(12), default="normal", nullable=False)

    # SLA / timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    first_response_due_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    resolution_due_at:     Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    first_response_at:     Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    resolved_at:           Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    closed_at:             Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    assigned_at:           Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    last_customer_msg_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    last_agent_msg_at:     Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)

    # Metrics
    reply_count:  Mapped[int]          = mapped_column(Integer, default=0, nullable=False)
    csat_score:   Mapped[Optional[int]] = mapped_column(Integer, default=None)  # 1..5
    csat_comment: Mapped[Optional[str]] = mapped_column(String(255), default=None)

    # ----------------------------- Relationships -----------------------------

    # Hii ina-match User.support_tickets (back_populates="user")
    user: Mapped["User"] = relationship(
        "User",
        back_populates="support_tickets",
        foreign_keys=[user_id],
        lazy="selectin",
    )

    # Hii ina-match User.support_tickets_assigned (back_populates="assignee")
    assignee: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="support_tickets_assigned",
        foreign_keys=[assigned_to],
        lazy="selectin",
    )

    messages: Mapped[List["SupportMessage"]] = relationship(
        "SupportMessage",
        back_populates="ticket",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # ----------------------------- Helpers -----------------------------

    def set_ticket_key(self, key: str) -> None:
        self.ticket_key = (key or "")[:24] or None

    def assign(self, user_id: Optional[int]) -> None:
        self.assigned_to = user_id
        self.assigned_at = dt.datetime.now(dt.timezone.utc) if user_id else None

    def set_priority(self, priority: str) -> None:
        p = (priority or "normal").lower()
        if p not in _ALLOWED_PRIORITY:
            raise ValueError("Invalid priority")
        self.priority = p

    def set_category(self, category: str) -> None:
        c = (category or "other").lower()
        if c not in _ALLOWED_CATEGORY:
            raise ValueError("Invalid category")
        self.category = c

    def set_status(self, status: str) -> None:
        s = (status or "open").lower()
        if s not in _ALLOWED_STATUS:
            raise ValueError("Invalid status")
        self.status = s
        now = dt.datetime.now(dt.timezone.utc)
        if s == "resolved":
            self.resolved_at = now
        if s == "closed":
            self.closed_at = now

    def mark_first_response(self) -> None:
        if not self.first_response_at:
            self.first_response_at = dt.datetime.now(dt.timezone.utc)

    def bump_reply(self, *, by_agent: bool) -> None:
        self.reply_count += 1
        now = dt.datetime.now(dt.timezone.utc)
        if by_agent:
            self.last_agent_msg_at = now
            self.mark_first_response()
            if self.status in {"open", "waiting_agent"}:
                self.status = "in_progress"
        else:
            self.last_customer_msg_at = now
            if self.status in {"open", "in_progress"}:
                self.status = "waiting_agent"

    def set_sla(
        self,
        *,
        first_response_due_at: Optional[dt.datetime],
        resolution_due_at: Optional[dt.datetime],
    ) -> None:
        self.first_response_due_at = first_response_due_at
        self.resolution_due_at = resolution_due_at

    def add_tags(self, items: Iterable[str]) -> None:
        s = set(self.tags or [])
        for t in items or []:
            t = str(t).strip().lower()
            if t:
                s.add(t)
        self.tags = sorted(s)

    def remove_tags(self, items: Iterable[str]) -> None:
        s = {x.lower() for x in (self.tags or [])}
        for t in items or []:
            s.discard(str(t).strip().lower())
        self.tags = sorted(s)

    def set_csat(self, score: int, comment: Optional[str] = None) -> None:
        if score < 1 or score > 5:
            raise ValueError("CSAT score must be 1..5")
        self.csat_score = score
        self.csat_comment = (comment or "")[:255] if comment else None

    def reopen(self) -> None:
        self.status = "open"
        self.closed_at = None
        self.resolved_at = None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SupportTicket id={self.id} key={self.ticket_key} status={self.status} priority={self.priority}>"

# ---------------------------- Ticket Messages ----------------------------

class SupportMessage(Base):
    """
    SupportMessage — threaded conversation for a support ticket.
    - Author can be user, agent, or system
    - Optional internal notes (is_internal=True)
    - Portable JSON attachments
    """
    __tablename__ = "support_messages"
    __table_args__ = (
        CheckConstraint("length(body) > 0", name="ck_support_msg_body_nonempty"),
        CheckConstraint("role IN ('user','agent','system')", name="ck_support_msg_role_enum"),
        Index("ix_support_msg_ticket_time", "ticket_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("support_tickets.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    author_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        default=None, index=True
    )

    role: Mapped[str] = mapped_column(String(8), default="user", nullable=False)  # user|agent|system
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    attachments: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, default=None)
    meta:         Mapped[Optional[Dict[str, Any]]]          = mapped_column(JSON, default=None)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    edited_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)

    ticket: Mapped["SupportTicket"] = relationship(
        "SupportTicket", back_populates="messages", lazy="selectin"
    )
    # eleza wazi foreign_keys ili kuondoa ukungu wa FK
    author: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[author_id], lazy="selectin"
    )

    # Helpers
    def edit(self, text: str) -> None:
        new_text = (text or "").strip()
        if not new_text:
            raise ValueError("Message body cannot be empty")
        self.body = new_text
        self.edited_at = dt.datetime.now(dt.timezone.utc)

    def add_attachment(self, url: str, name: Optional[str] = None, size: Optional[int] = None) -> None:
        files = list(self.attachments or [])
        files.append({"url": url, "name": name, "size": size})
        self.attachments = files

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SupportMessage id={self.id} ticket={self.ticket_id} role={self.role} internal={self.is_internal}>"

__all__ = ["SupportTicket", "SupportMessage"]
