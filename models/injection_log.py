# backend/models/injection_log.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import hashlib
import datetime as dt
from typing import Optional, TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method
from sqlalchemy.ext.mutable import MutableDict, MutableList

from backend.db import Base
from backend.models._types import JSON_VARIANT  # portable JSON (PG: JSONB, others: JSON)

if TYPE_CHECKING:
    from .user import User


# ---------- Enums ----------
class InjectSource(str, enum.Enum):
    agent = "agent"      # in-app AI agent
    api   = "api"        # external API call
    cli   = "cli"        # dev tool / script
    web   = "web"        # web UI
    other = "other"

class InjectAction(str, enum.Enum):
    create  = "create"
    update  = "update"
    delete  = "delete"
    patch   = "patch"
    run     = "run"      # execute script/task
    analyze = "analyze"

class InjectStatus(str, enum.Enum):
    success = "success"
    failed  = "failed"
    skipped = "skipped"

class Environment(str, enum.Enum):
    prod    = "prod"
    staging = "staging"
    dev     = "dev"
    test    = "test"
    unknown = "unknown"

class TargetType(str, enum.Enum):
    file     = "file"
    config   = "config"
    service  = "service"
    database = "database"
    function = "function"
    workflow = "workflow"
    other    = "other"

class ActorType(str, enum.Enum):
    user     = "user"      # human
    service  = "service"   # service account
    system   = "system"    # internal job/cron
    unknown  = "unknown"


class InjectionLog(Base):
    """
    Kumbukumbu ya 'AI injection' yenye muktadha mpana:
    - AuthZ/Actor: actor_type, scopes, requires_approval
    - Observability: bytes/tokens in/out, warnings, labels
    - Safety: redactions, scrubbed_preview, fingerprint & hashes
    - Reliability: retries, parent/batch chaining, rollback_ref
    """
    __tablename__ = "injection_logs"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_injlog_idem_key"),
        CheckConstraint("length(content) >= 1", name="ck_injlog_content_len"),
        CheckConstraint("retry_attempt >= 0", name="ck_injlog_retry_nonneg"),
        CheckConstraint("bytes_in  >= 0 AND bytes_out  >= 0", name="ck_injlog_bytes_nonneg"),
        CheckConstraint("tokens_in >= 0 AND tokens_out >= 0", name="ck_injlog_tokens_nonneg"),
        # query patterns
        Index("ix_injlog_user_time", "user_id", "created_at"),
        Index("ix_injlog_status_time", "status", "created_at"),
        Index("ix_injlog_action_target", "action", "target"),
        Index("ix_injlog_env_source", "environment", "source"),
        Index("ix_injlog_request", "request_id", "correlation_id"),
        Index("ix_injlog_tag", "tag"),
        Index("ix_injlog_workspace", "workspace", "project"),
        Index("ix_injlog_batch", "batch_id", "created_at"),
        Index("ix_injlog_parent", "parent_id"),
        Index("ix_injlog_actor", "actor_type"),
        Index("ix_injlog_approved", "requires_approval", "approved_by_user_id"),
        Index("ix_injlog_tool", "tool"),
        Index("ix_injlog_target_type", "target_type", "target"),
        Index("ix_injlog_fp_minute", "fingerprint", "minute_bucket"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Who/Context
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    user: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[user_id], passive_deletes=True, lazy="selectin"
    )

    actor_type: Mapped[ActorType] = mapped_column(
        SQLEnum(ActorType, name="inject_actor_type", native_enum=False, validate_strings=True),
        default=ActorType.unknown, nullable=False, index=True
    )
    scopes: Mapped[Optional[list]] = mapped_column(MutableList.as_mutable(JSON_VARIANT))  # ["fs:read", "repo:write"]
    tag: Mapped[Optional[str]] = mapped_column(String(100), index=True)  # e.g. "smartinject:repoA"

    source: Mapped[InjectSource] = mapped_column(
        SQLEnum(InjectSource, name="inject_source", native_enum=False, validate_strings=True),
        default=InjectSource.agent, nullable=False, index=True
    )
    environment: Mapped[Environment] = mapped_column(
        SQLEnum(Environment, name="inject_environment", native_enum=False, validate_strings=True),
        default=Environment.unknown, nullable=False, index=True
    )

    # Workspace / grouping
    workspace: Mapped[Optional[str]] = mapped_column(String(120), index=True)  # org/workspace key
    project:   Mapped[Optional[str]] = mapped_column(String(160), index=True)  # repo/app/service name
    batch_id:  Mapped[Optional[str]] = mapped_column(String(64), index=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("injection_logs.id", ondelete="SET NULL"), index=True)

    # What was targeted
    action: Mapped[InjectAction] = mapped_column(
        SQLEnum(InjectAction, name="inject_action", native_enum=False, validate_strings=True),
        default=InjectAction.update, nullable=False, index=True
    )
    target_type: Mapped[TargetType] = mapped_column(
        SQLEnum(TargetType, name="inject_target_type", native_enum=False, validate_strings=True),
        default=TargetType.other, nullable=False, index=True
    )
    target:   Mapped[Optional[str]] = mapped_column(String(400), index=True)  # path/resource identifier
    language: Mapped[Optional[str]] = mapped_column(String(40))              # "python", "sql", "bash"
    tool:     Mapped[Optional[str]] = mapped_column(String(80), index=True)  # executor/tool plugin name

    # Payloads & results
    content: Mapped[str] = mapped_column(Text, nullable=False)               # input/prompt/patch body (sanitized)
    diff:    Mapped[Optional[str]] = mapped_column(Text)                     # unified diff (optional)
    patch:   Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))  # structured patch

    status:  Mapped[InjectStatus] = mapped_column(
        SQLEnum(InjectStatus, name="inject_status", native_enum=False, validate_strings=True),
        default=InjectStatus.success, nullable=False, index=True
    )
    message: Mapped[Optional[str]] = mapped_column(String(400))              # short outcome summary

    error_type:    Mapped[Optional[str]] = mapped_column(String(120), index=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    trace:         Mapped[Optional[str]] = mapped_column(Text)
    warnings:      Mapped[Optional[list]] = mapped_column(MutableList.as_mutable(JSON_VARIANT))  # ["lint:unused-var", ...]

    # Integrity / analytics
    before_hash:  Mapped[Optional[str]] = mapped_column(String(64), index=True)  # sha256 of content before
    after_hash:   Mapped[Optional[str]] = mapped_column(String(64), index=True)  # sha256 of content after
    fingerprint:  Mapped[Optional[str]] = mapped_column(String(64), index=True)  # sha256(action|target|before_hash)
    minute_bucket: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )

    # Security / safety
    requires_approval:  Mapped[bool] = mapped_column(Integer, nullable=False, server_default=text("0"))  # boolean-ish for SQLite
    approved_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    approved_by: Mapped[Optional["User"]] = relationship("User", foreign_keys=[approved_by_user_id], lazy="selectin")
    dry_run:     Mapped[bool] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rollback_ref: Mapped[Optional[str]] = mapped_column(String(160), index=True)  # commit/tag/snapshot id

    redactions:       Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))  # {"secrets":["AKIA...","sk-..."]}
    scrubbed_preview: Mapped[Optional[str]] = mapped_column(Text)                                     # safe snippet for UI
    labels:           Mapped[Optional[list]] = mapped_column(MutableList.as_mutable(JSON_VARIANT))   # ["safe","autosync"]

    # Metering / retries
    retry_attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    bytes_in:   Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    bytes_out:  Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tokens_in:  Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Request correlation
    request_id:     Mapped[Optional[str]] = mapped_column(String(64), index=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(100), unique=True, index=True)

    # Extras
    meta:   Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))    # {"repo":"...", "branch":"..."}
    params: Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))    # runtime parameters
    client_ip:  Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(String(400))

    # Timing
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    started_at:  Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # ---------- Hybrids ----------
    @hybrid_property
    def duration_ms(self) -> Optional[int]:
        if not self.finished_at:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @hybrid_property
    def is_success(self) -> bool:
        return self.status == InjectStatus.success

    @hybrid_property
    def has_error(self) -> bool:
        return self.status == InjectStatus.failed or bool(self.error_type or self.error_message)

    # ---------- Helpers ----------
    def _sha256(self, data: str | bytes | None) -> Optional[str]:
        if not data:
            return None
        if isinstance(data, str):
            data = data.encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def compute_fingerprint(self) -> str:
        """sha256(f"{action}|{target}|{before_hash or ''}") — kwa dedupe/analytics."""
        base = f"{self.action}|{self.target or ''}|{self.before_hash or ''}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    def ensure_fingerprint(self) -> None:
        if not self.fingerprint:
            self.fingerprint = self.compute_fingerprint()

    def start(self, *, now: Optional[dt.datetime] = None) -> None:
        self.started_at = now or dt.datetime.now(dt.timezone.utc)
        # minute bucket ya observability (minute floor)
        ts = self.started_at
        self.minute_bucket = ts.replace(second=0, microsecond=0)

    def finish_ok(
        self,
        *,
        after_hash: str | None = None,
        message: str | None = None,
        bytes_out: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        self.status = InjectStatus.success
        self.finished_at = dt.datetime.now(dt.timezone.utc)
        if after_hash:
            self.after_hash = after_hash
        if message:
            self.message = message[:400]
        if bytes_out is not None:
            self.bytes_out = max(0, int(bytes_out))
        if tokens_out is not None:
            self.tokens_out = max(0, int(tokens_out))
        self.ensure_fingerprint()

    def finish_error(
        self,
        *,
        error_type: str | None = None,
        error_message: str | None = None,
        trace: str | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self.status = InjectStatus.failed
        self.finished_at = dt.datetime.now(dt.timezone.utc)
        if error_type:
            self.error_type = error_type[:120]
        if error_message:
            self.error_message = error_message
        if trace:
            self.trace = trace
        if warnings:
            self.warnings = list(warnings)
        self.ensure_fingerprint()

    def increment_retry(self) -> int:
        self.retry_attempt = (self.retry_attempt or 0) + 1
        return self.retry_attempt

    def set_metering(self, *, bytes_in: int | None = None, tokens_in: int | None = None) -> None:
        if bytes_in is not None:
            self.bytes_in = max(0, int(bytes_in))
        if tokens_in is not None:
            self.tokens_in = max(0, int(tokens_in))

    def set_hashes(self, *, before: str | None = None, after: str | None = None) -> None:
        if before:
            self.before_hash = before
        if after:
            self.after_hash = after

    def redact_secrets(self, *secrets: str, mask: str = "•••") -> None:
        """Andaa 'scrubbed_preview' isiyo na siri kutoka kwenye content/diff."""
        to_scrub = [s for s in secrets if s]
        preview = (self.diff or self.content or "")[:20000]
        for s in to_scrub:
            preview = preview.replace(s, mask)
        self.scrubbed_preview = preview
        self.redactions = {"secrets": list(to_scrub), "mask": mask}

    def truncate_big_fields(self, *, max_len: int = 100_000) -> None:
        """Kata content/diff/trace ili kulinda DB & UI."""
        if self.content and len(self.content) > max_len:
            self.content = self.content[:max_len]
        if self.diff and len(self.diff) > max_len:
            self.diff = self.diff[:max_len]
        if self.trace and len(self.trace) > max_len:
            self.trace = self.trace[:max_len]

    def approve(self, by_user_id: int | None) -> None:
        self.requires_approval = 0  # false
        self.approved_by_user_id = by_user_id

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<InjectionLog id={self.id} action={self.action} target={self.target!r} "
                f"status={self.status} env={self.environment} workspace={self.workspace}>")

    # ---------- Validators ----------
    @validates("tag", "workspace", "project", "tool", "rollback_ref", "target", "language")
    def _v_strip(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @validates("idempotency_key", "request_id", "correlation_id")
    def _v_len64_100(self, key: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if key == "idempotency_key" and len(v) > 100:
            return v[:100]
        if len(v) > 64 and key != "idempotency_key":
            return v[:64]
        return v

    @validates("error_type")
    def _v_error_type(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v[:120] if v else None
