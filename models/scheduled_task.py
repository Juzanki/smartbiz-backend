# backend/models/scheduled_task.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
ScheduledTask (imeboreshwa):
- Retries (exponential backoff + jitter) na visibility lock.
- Idempotency & dedupe (idempotency_key + payload_hash).
- Queue/priorities + due querying (next_attempt_at).
- Audit & soft-delete; client/device metadata.
- Indexes/constraints ziko ndani ya __table_args__ (msingi wa Alembic).
"""

import datetime as dt
import enum
import uuid
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import text as sa_text

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    from .task_failure_log import TaskFailureLog


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# -------------------- Enums --------------------
class TaskType(str, enum.Enum):
    message = "message"
    email = "email"
    post = "post"
    webhook = "webhook"
    other = "other"


class TaskStatus(str, enum.Enum):
    pending = "pending"      # imesubiri kukimbiwa
    running = "running"      # imefungwa na worker (visibility lock)
    sent = "sent"            # imekamilika
    failed = "failed"        # imeshindikana (retries zimeisha au kosa la kudumu)
    cancelled = "cancelled"  # imebatilishwa


class TaskPriority(str, enum.Enum):
    low = "low"
    normal = "normal"
    high = "high"
    critical = "critical"


# -------------------- Model --------------------
class ScheduledTask(Base):
    """
    Kazi iliyopangwa kukimbiwa wakati fulani (ujumbe, barua pepe, webhook, n.k.).
    Ina “mobile-aware” fields (platform/version/device/network) na taratibu thabiti za kufunga kazi.
    """
    __tablename__ = "scheduled_tasks"
    __mapper_args__ = {"eager_defaults": True}

    # Identity
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    public_id: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, default=lambda: str(uuid.uuid4()), nullable=False,
        doc="UUID ya umma kwa marejeo salama kwenye API/clients."
    )

    # Owner
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    user: Mapped["User"] = relationship("User", back_populates="scheduled_tasks", lazy="selectin", passive_deletes=True)

    # Classification
    type: Mapped[TaskType] = mapped_column(
        SQLEnum(TaskType, name="task_type", native_enum=False, validate_strings=True),
        default=TaskType.message, nullable=False, index=True,
    )
    queue: Mapped[str] = mapped_column(String(64), default="default", nullable=False, index=True)
    priority: Mapped[TaskPriority] = mapped_column(
        SQLEnum(TaskPriority, name="task_priority", native_enum=False, validate_strings=True),
        default=TaskPriority.normal, nullable=False, index=True,
    )

    # Payload/content
    content: Mapped[str] = mapped_column(Text, nullable=False, doc="Mwili wa ujumbe/maudhui ya kazi.")
    payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    payload_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True, doc="SHA-256/xxh3 ya payload.")

    # Target (hutoa hint kwa handler husika)
    target: Mapped[Optional[str]] = mapped_column(String(255), index=True)

    # Scheduling
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    scheduled_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False, server_default=func.now(),
        doc="Jaribio lijalo; la kwanza mara nyingi = scheduled_time."
    )
    deadline_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # State
    status: Mapped[TaskStatus] = mapped_column(
        SQLEnum(TaskStatus, name="task_status", native_enum=False, validate_strings=True),
        default=TaskStatus.pending, index=True, nullable=False,
    )
    retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("5"))

    # Locking / visibility timeout
    locked_by: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    lock_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    lock_token: Mapped[Optional[str]] = mapped_column(String(36))

    # Idempotency
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), index=True)

    # Audit / lifecycle
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64))
    last_error_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Soft delete
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Optimistic locking
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))

    # Client / mobile metadata
    client_platform: Mapped[Optional[str]] = mapped_column(String(32))   # ios | android | web | service
    app_version: Mapped[Optional[str]] = mapped_column(String(32))
    device_model: Mapped[Optional[str]] = mapped_column(String(64))
    network_type: Mapped[Optional[str]] = mapped_column(String(16))      # wifi | 4g | 5g | other

    # Relationships
    failure_logs: Mapped[list["TaskFailureLog"]] = relationship(
        "TaskFailureLog", back_populates="task", cascade="all, delete-orphan", lazy="selectin"
    )

    # ---------- Indexes / Constraints ----------
    __table_args__ = (
        # Due queries & scheduling
        Index("ix_scheduled_tasks_due", "status", "next_attempt_at"),
        Index("ix_scheduled_tasks_queue_priority", "queue", "priority", "next_attempt_at"),
        Index("ix_scheduled_tasks_user_status", "user_id", "status"),
        Index("ix_scheduled_tasks_lock", "locked_by", "lock_expires_at"),
        # Dedupe / idempotency
        UniqueConstraint("user_id", "type", "idempotency_key", name="uq_task_idem_user_type"),
        # Guards
        CheckConstraint("retries >= 0", name="ck_task_retries_nonneg"),
        CheckConstraint("max_retries >= 0", name="ck_task_max_retries_nonneg"),
        CheckConstraint("length(content) >= 1", name="ck_task_content_nonempty"),
    )

    # -------------------- Representations --------------------
    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ScheduledTask id={self.id} public_id={self.public_id} "
            f"user={self.user_id} type={self.type.value} status={self.status.value} "
            f"queue={self.queue} prio={self.priority.value} next={self.next_attempt_at}>"
        )

    # -------------------- Convenience properties --------------------
    @property
    def is_terminal(self) -> bool:
        return self.status in {TaskStatus.sent, TaskStatus.failed, TaskStatus.cancelled}

    @property
    def is_locked(self) -> bool:
        return bool(self.lock_expires_at and self.lock_expires_at > _utcnow())

    @property
    def is_due(self) -> bool:
        """
        True kama kazi inapaswa kukimbia sasa na haiko kwenye hali ya mwisho.
        Inatumia next_attempt_at (si scheduled_time) kwa retries/backoff.
        """
        if self.is_terminal:
            return False
        if not self.next_attempt_at or self.next_attempt_at.tzinfo is None:
            return False
        return self.next_attempt_at <= _utcnow()

    # -------------------- Query helpers --------------------
    @classmethod
    def due_filter(cls):
        """
        SQL expression ya due-tasks zisizofungwa au lock iliyokwisha muda.
        Tumia: session.query(ScheduledTask).filter(*ScheduledTask.due_filter())
        """
        now = func.now()
        return (
            cls.status.in_([TaskStatus.pending, TaskStatus.running]),   # running but visibility may expire
            cls.next_attempt_at <= now,
            ((cls.lock_expires_at.is_(None)) | (cls.lock_expires_at <= now)),
        )

    # -------------------- State transitions --------------------
    def mark_running(self, worker_id: str, visibility_timeout: int = 300) -> None:
        """
        Funga kazi na uweke visibility timeout.
        **Tumia ndani ya transaction** yenye guard ya WHERE:
        - status IN (pending, running)
        - (lock_expires_at IS NULL OR lock_expires_at <= now())
        """
        now = _utcnow()
        self.status = TaskStatus.running
        self.locked_by = worker_id
        self.lock_token = str(uuid.uuid4())
        self.lock_expires_at = now + dt.timedelta(seconds=max(1, visibility_timeout))
        if self.next_attempt_at is None:
            self.next_attempt_at = now
        self.version = (self.version or 0) + 1

    def mark_sent(self) -> None:
        self.status = TaskStatus.sent
        self.sent_at = _utcnow()
        self.locked_by = None
        self.lock_expires_at = None
        self.lock_token = None
        self.version = (self.version or 0) + 1

    def _compute_backoff_seconds(self, retry_num: int, base: int = 2, cap: int = 3600) -> int:
        """
        Exponential backoff na jitter ndogo: min(cap, base^retry + rand[0..retry*3]).
        """
        import random
        exp = min(cap, base ** min(retry_num, 12))  # linda overflow
        jitter = random.randint(0, max(1, retry_num * 3))
        return int(min(cap, exp + jitter))

    def mark_failed(self, error_msg: str, error_code: Optional[str] = None) -> None:
        self.last_error = (error_msg or "")[:2000]
        self.last_error_code = error_code
        self.last_error_at = _utcnow()
        self.retries = (self.retries or 0) + 1

        if (self.retries or 0) > (self.max_retries or 0):
            self.status = TaskStatus.failed
            self.locked_by = None
            self.lock_expires_at = None
            self.lock_token = None
        else:
            delay = self._compute_backoff_seconds(self.retries)
            self.next_attempt_at = _utcnow() + dt.timedelta(seconds=delay)
            self.status = TaskStatus.pending
        self.version = (self.version or 0) + 1

    def cancel(self, reason: Optional[str] = None) -> None:
        self.status = TaskStatus.cancelled
        if reason:
            self.last_error = reason[:2000]
            self.last_error_code = "CANCELLED"
            self.last_error_at = _utcnow()
        self.locked_by = None
        self.lock_expires_at = None
        self.lock_token = None
        self.version = (self.version or 0) + 1

    def reschedule(self, when: dt.datetime, reset_retries: bool = False) -> None:
        assert when.tzinfo is not None, "Reschedule requires timezone-aware datetime"
        self.scheduled_time = when
        self.next_attempt_at = when
        if reset_retries:
            self.retries = 0
            self.status = TaskStatus.pending
        self.version = (self.version or 0) + 1

    def clear_lock_if_expired(self) -> None:
        if self.lock_expires_at and self.lock_expires_at <= _utcnow():
            self.locked_by = None
            self.lock_expires_at = None
            self.lock_token = None
            self.version = (self.version or 0) + 1
