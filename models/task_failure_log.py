# backend/models/task_failure_log.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import hashlib
import traceback
from typing import Optional, Dict, Any, Iterable

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

class TaskFailureLog(Base):
    """
    TaskFailureLog — production-grade failure telemetry for scheduled/background jobs.

    Mobile-first upgrades:
    - SQLAlchemy 2.0 typed mappings
    - Rich context (run_id, attempt, service/env, correlation/request IDs)
    - Transient flag + next_retry_at to coordinate retries
    - Fingerprinting for de-duplication/alert grouping
    - Optional stack trace & JSON payload
    - Tight indexes for hot paths (by task, day, retry queue)
    - Helpers: from_exception, schedule_retry, redact_details, to_public_dict
    """
    __tablename__ = "task_failure_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Link to your scheduler's task row
    task_id: Mapped[int] = mapped_column(
        ForeignKey("scheduled_tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # Lightweight denorm for faster queries / dashboards
    task_name: Mapped[Optional[str]] = mapped_column(String(128), default=None, index=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(64), default=None, index=True)  # per-execution id
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)            # 1-based

    # Error envelope
    severity: Mapped[str] = mapped_column(
        String(16), default="error", nullable=False, doc="debug|info|warning|error|critical"
    )
    error_type: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    error_code: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    stack_trace: Mapped[Optional[str]] = mapped_column(Text, default=None)

    # Machine-readable payload / context
    details: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)

    # Grouping & routing
    fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), default=None, index=True, doc="sha256(error_type|message[:256]|task_name)"
    )
    service: Mapped[Optional[str]] = mapped_column(String(64), default=None)     # e.g., "worker", "cron"
    environment: Mapped[Optional[str]] = mapped_column(String(32), default=None) # e.g., "prod", "staging"
    correlation_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, default=None)
    request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, default=None)

    # Retry coordination
    is_transient: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_retry_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None, index=True)

    # Time buckets (for cheap charts)
    day_bucket: Mapped[dt.date] = mapped_column(Date, server_default=func.current_date(), nullable=False, index=True)

    # Timestamps (TZ-aware)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    # Relationship to scheduled task
    task: Mapped["ScheduledTask"] = relationship("ScheduledTask", back_populates="failure_logs", lazy="selectin")

    __table_args__ = (
        CheckConstraint("attempt >= 1", name="ck_task_fail_attempt_min1"),
        CheckConstraint(
            "severity in ('debug','info','warning','error','critical')",
            name="ck_task_fail_severity_enum"
        ),
        Index("ix_task_failure_logs_task_created", "task_id", "created_at"),
        Index("ix_task_failure_logs_retry_queue", "is_transient", "next_retry_at"),
        Index("ix_task_failure_logs_fingerprint_day", "fingerprint", "day_bucket"),
        UniqueConstraint("run_id", "attempt", name="uq_task_fail_run_attempt"),
    )

    # -------------------------- Helpers (no DB I/O here) --------------------------

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    @classmethod
    def from_exception(
        cls,
        *,
        task_id: int,
        task_name: Optional[str],
        exc: BaseException,
        run_id: Optional[str] = None,
        attempt: int = 1,
        service: Optional[str] = None,
        environment: Optional[str] = None,
        correlation_id: Optional[str] = None,
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        transient_default: bool = True,
        severity: str = "error",
        include_stack: bool = True,
    ) -> "TaskFailureLog":
        """Construct a log entry from an exception safely."""
        etype = exc.__class__.__name__
        emsg = str(exc)[:2000]  # bound for safety
        stack = None
        if include_stack:
            stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[:10000]

        # Compose fingerprint for grouping
        basis = f"{etype}|{emsg[:256]}|{task_name or ''}"
        fp = cls._sha256(basis)

        return cls(
            task_id=task_id,
            task_name=(task_name or "")[:128] or None,
            run_id=(run_id or "")[:64] or None,
            attempt=max(1, attempt),
            severity=(severity or "error"),
            error_type=etype[:64],
            error_code=None,
            error_message=emsg,
            stack_trace=stack,
            details=details,
            fingerprint=fp,
            service=(service or "")[:64] or None,
            environment=(environment or "")[:32] or None,
            correlation_id=(correlation_id or "")[:64] or None,
            request_id=(request_id or "")[:64] or None,
            is_transient=bool(transient_default),
        )

    def schedule_retry(self, *, base_seconds: int = 30, jitter_seconds: int = 0) -> None:
        """
        Set `next_retry_at` using exponential backoff:
        delay = base_seconds * (2 ** max(attempt-1, 0)) + jitter_seconds
        """
        base = max(0, int(base_seconds))
        factor = 2 ** max(self.attempt - 1, 0)
        delay = base * factor + max(0, int(jitter_seconds))
        self.next_retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay)

    def mark_non_transient(self) -> None:
        """Disable retries for permanent failures."""
        self.is_transient = False
        self.next_retry_at = None

    def redact_details(self, keys: Iterable[str] = ("password", "secret", "token")) -> None:
        """Mask sensitive values in `details` if present."""
        if not self.details:
            return
        redacted = dict(self.details)
        for k in keys:
            if k in redacted and redacted[k] is not None:
                redacted[k] = "***"
        self.details = redacted

    def to_public_dict(self) -> Dict[str, Any]:
        """Compact, mobile-friendly projection for API responses."""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "severity": self.severity,
            "error_type": self.error_type,
            "error_code": self.error_code,
            "message": self.error_message[:160],  # trimmed for lists
            "is_transient": self.is_transient,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<TaskFailureLog id={self.id} task={self.task_id} "
            f"name={self.task_name} attempt={self.attempt} sev={self.severity}>"
        )

# ------------------------ Daily aggregation for dashboards ------------------------

class TaskFailureDaily(Base):
    """
    TaskFailureDaily — per-task/day rollup for fast mobile dashboards.
    Maintain via a cron or background job aggregating TaskFailureLog by (task_id, day_bucket).
    """
    __tablename__ = "task_failure_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("scheduled_tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    task_name: Mapped[Optional[str]] = mapped_column(String(128), default=None, index=True)
    day_bucket: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)

    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    critical: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warning: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retriable: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    non_transient: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    task: Mapped["ScheduledTask"] = relationship("ScheduledTask", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("task_id", "day_bucket", name="uq_task_failure_daily_task_day"),
        Index("ix_task_failure_daily_hot", "day_bucket", "total"),
    )

    def bump(
        self,
        *,
        severity: str,
        retriable: bool,
        count: int = 1,
    ) -> None:
        """Increment counters by severity and retry capability."""
        n = max(0, int(count))
        self.total += n
        sev = (severity or "").lower()
        if sev == "critical":
            self.critical += n
        elif sev == "error":
            self.error += n
        elif sev == "warning":
            self.warning += n
        if retriable:
            self.retriable += n
        else:
            self.non_transient += n



