# backend/models/audit_log.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, Any, Mapping, Dict, TYPE_CHECKING

from sqlalchemy import (
    Enum as SQLEnum,
    Integer,
    String,
    DateTime,
    ForeignKey,
    func,
    Index,
    CheckConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, Session
from sqlalchemy.event import listens_for
from sqlalchemy.ext.mutable import MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

if TYPE_CHECKING:
    from .user import User


class AuditAction(str, enum.Enum):
    # Auth
    login = "login"
    logout = "logout"
    password_reset = "password_reset"
    signup = "signup"
    # Profile & settings
    update_profile = "update_profile"
    change_password = "change_password"
    update_settings = "update_settings"
    # Business domain
    create_order = "create_order"
    update_order = "update_order"
    delete_order = "delete_order"
    send_gift = "send_gift"
    start_stream = "start_stream"
    end_stream = "end_stream"
    # System
    webhook_call = "webhook_call"
    admin_action = "admin_action"
    other = "other"


class AuditLog(Base):
    """
    Immutable, append-only record of notable user/system actions.
    Keep rows lean; put heavy detail in `meta` (JSON).
    """
    __tablename__ = "audit_logs"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Optional (allow logs to remain after user deletion)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Classification
    action: Mapped[AuditAction] = mapped_column(
        SQLEnum(AuditAction, name="audit_action_enum", native_enum=False, validate_strings=True),
        nullable=False,
        index=True,
    )

    # Optional target/resource for quick lookups
    resource_type: Mapped[Optional[str]] = mapped_column(String(64), index=True)   # e.g. "order","stream","product"
    resource_id:   Mapped[Optional[str]] = mapped_column(String(64), index=True)   # external or internal id

    # Request context (best-effort)
    ip_address:  Mapped[Optional[str]] = mapped_column(String(45))                 # IPv4/IPv6
    user_agent:  Mapped[Optional[str]] = mapped_column(String(255))
    session_id:  Mapped[Optional[str]] = mapped_column(String(64), index=True)
    request_id:  Mapped[Optional[str]] = mapped_column(String(64), index=True)     # correlation/trace id
    method:      Mapped[Optional[str]] = mapped_column(String(8))                  # GET/POST/...
    path:        Mapped[Optional[str]] = mapped_column(String(200))

    # Idempotency / dedup (optional unique key you can set in services)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True)

    # Extra details (flat keys recommended)
    meta: Mapped[Dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT), default=dict)

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Relationship back to User
    user: Mapped[Optional["User"]] = relationship(
        "User",
        backref="audit_logs",
        foreign_keys=lambda: [AuditLog.user_id],
        passive_deletes=True,
        lazy="selectin",
    )

    __table_args__ = (
        # useful composite indexes
        Index("ix_audit_user_time", "user_id", "created_at"),
        Index("ix_audit_action_time", "action", "created_at"),
        Index("ix_audit_resource_ref", "resource_type", "resource_id"),
        Index("ix_audit_session_time", "session_id", "created_at"),
        Index("ix_audit_request_time", "request_id", "created_at"),
        # guards
        CheckConstraint(
            "resource_type IS NULL OR length(trim(resource_type)) > 0",
            name="ck_audit_resource_type_nonempty",
        ),
        CheckConstraint(
            "resource_id IS NULL OR length(trim(resource_id)) > 0",
            name="ck_audit_resource_id_nonempty",
        ),
    )

    # ---------- Service helpers ----------

    @classmethod
    def log(
        cls,
        session: Session,
        *,
        action: AuditAction | str,
        user_id: Optional[int] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        method: Optional[str] = None,
        path: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> "AuditLog":
        """
        Tengeneza rekodi mpya ya audit (haina commit).
        - Tumia `idempotency_key` kuzuia duplicates kutoka retries.
        """
        entry = cls(
            user_id=user_id,
            action=AuditAction(str(action)) if not isinstance(action, AuditAction) else action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip_address,
            user_agent=user_agent,
            session_id=session_id,
            request_id=request_id,
            method=method,
            path=path,
            idempotency_key=(idempotency_key.strip()[:64] if idempotency_key else None),
            meta=dict(meta or {}),
        )
        session.add(entry)
        return entry

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<AuditLog id={self.id} user_id={self.user_id} action={self.action} "
            f"res={self.resource_type}:{self.resource_id} at={self.created_at}>"
        )


# --------- Normalizers / Guards ---------
@listens_for(AuditLog, "before_insert")
def _audit_before_insert(_m, _c, t: AuditLog) -> None:
    # trims & size caps
    if t.resource_type:
        t.resource_type = t.resource_type.strip()[:64] or None
    if t.resource_id:
        t.resource_id = t.resource_id.strip()[:64] or None
    if t.ip_address:
        t.ip_address = t.ip_address.strip()[:45] or None
    if t.user_agent:
        t.user_agent = t.user_agent.strip()[:255] or None
    if t.session_id:
        t.session_id = t.session_id.strip()[:64] or None
    if t.request_id:
        t.request_id = t.request_id.strip()[:64] or None
    if t.method:
        t.method = t.method.strip().upper()[:8] or None
    if t.path:
        t.path = t.path.strip()[:200] or None
    if t.idempotency_key:
        t.idempotency_key = t.idempotency_key.strip()[:64] or None


@listens_for(AuditLog, "before_update")
def _audit_before_update(_m, _c, t: AuditLog) -> None:
    # enforce immutability-ish: avoid changing key identity fields accidentally
    if t.id is not None:
        # you can extend this with attribute history checks if desired
        pass
    # re-apply trims
    if t.resource_type:
        t.resource_type = t.resource_type.strip()[:64] or None
    if t.resource_id:
        t.resource_id = t.resource_id.strip()[:64] or None
    if t.ip_address:
        t.ip_address = t.ip_address.strip()[:45] or None
    if t.user_agent:
        t.user_agent = t.user_agent.strip()[:255] or None
    if t.session_id:
        t.session_id = t.session_id.strip()[:64] or None
    if t.request_id:
        t.request_id = t.request_id.strip()[:64] or None
    if t.method:
        t.method = t.method.strip().upper()[:8] or None
    if t.path:
        t.path = t.path.strip()[:200] or None
    if t.idempotency_key:
        t.idempotency_key = t.idempotency_key.strip()[:64] or None
