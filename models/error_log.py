# backend/models/error_log.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import hashlib
import datetime as dt
import enum
from typing import Optional, Mapping, Any, TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method
from sqlalchemy.event import listens_for
from sqlalchemy.ext.mutable import MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

if TYPE_CHECKING:
    from .user import User


# ---------- Enums ----------
class ErrorSource(str, enum.Enum):
    frontend    = "frontend"
    backend     = "backend"
    integration = "integration"
    job         = "job"
    other       = "other"


class ErrorSeverity(str, enum.Enum):
    info     = "info"
    warning  = "warning"
    error    = "error"
    critical = "critical"


class Environment(str, enum.Enum):
    prod    = "prod"
    staging = "staging"
    dev     = "dev"
    test    = "test"
    unknown = "unknown"


# ---------- Helpers ----------
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}\-[0-9a-fA-F]{4}\-[1-5][0-9a-fA-F]{3}\-[89abAB][0-9a-fA-F]{3}\-[0-9a-fA-F]{12}\b"
)
_NUM_RE = re.compile(r"\b\d{3,}\b")
_WS_RE = re.compile(r"\s+")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _minute_bucket(ts: dt.datetime) -> dt.datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.replace(second=0, microsecond=0)


def _sanitize_for_fingerprint(text: str) -> str:
    if not text:
        return ""
    t = _UUID_RE.sub("<uuid>", text)
    t = _NUM_RE.sub("<num>", t)
    t = _WS_RE.sub(" ", t).strip()
    return t[:400]


class ErrorLog(Base):
    """
    Logs za makosa kwa uchambuzi/alerting.
    - Fingerprint PII-safe
    - minute_bucket kwa analytics
    - Context ya HTTP, service, n.k.
    """
    __tablename__ = "error_logs"
    __mapper_args__ = {"eager_defaults": True}

    # ---------- Identity / Who ----------
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # ---------- Classifiers ----------
    source: Mapped[ErrorSource] = mapped_column(
        SQLEnum(ErrorSource, name="error_source", native_enum=False, validate_strings=True),
        default=ErrorSource.backend, nullable=False, index=True,
    )
    severity: Mapped[ErrorSeverity] = mapped_column(
        SQLEnum(ErrorSeverity, name="error_severity", native_enum=False, validate_strings=True),
        default=ErrorSeverity.error, nullable=False, index=True,
    )
    environment: Mapped[Environment] = mapped_column(
        SQLEnum(Environment, name="error_environment", native_enum=False, validate_strings=True),
        default=Environment.unknown, nullable=False, index=True,
    )

    # ---------- What ----------
    error_type: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    trace: Mapped[Optional[str]] = mapped_column(Text)

    # ---------- HTTP / request context ----------
    http_method: Mapped[Optional[str]] = mapped_column(String(10))
    http_path: Mapped[Optional[str]] = mapped_column(String(400), index=True)
    http_status: Mapped[Optional[int]] = mapped_column(Integer)
    user_agent: Mapped[Optional[str]] = mapped_column(String(400))
    client_ip: Mapped[Optional[str]] = mapped_column(String(64))
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)

    request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # ---------- Service / build context ----------
    service: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    component: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    release: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    node: Mapped[Optional[str]] = mapped_column(String(80))
    client_os: Mapped[Optional[str]] = mapped_column(String(80))
    client_app: Mapped[Optional[str]] = mapped_column(String(80))

    # ---------- Analytics / extra ----------
    fingerprint: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    minute_bucket: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True,
    )
    tags: Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))
    meta: Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))

    # ---------- When ----------
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"), nullable=False,
    )

    # ---------- Relationship ----------
    # FIX: tumia Column object, si string, kwenye foreign_keys
    user: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="error_logs",
        foreign_keys=[user_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # ---------- Hybrids / helpers ----------
    @hybrid_property
    def is_critical(self) -> bool:
        return self.severity == ErrorSeverity.critical

    @hybrid_property
    def is_client_error(self) -> bool:
        s = self.http_status or 0
        return 400 <= s <= 499

    @hybrid_property
    def is_server_error(self) -> bool:
        s = self.http_status or 0
        return 500 <= s <= 599

    @hybrid_method
    def has_tag(self, key: str, value: str | None = None) -> bool:
        if not self.tags:
            return False
        return key in self.tags if value is None else self.tags.get(key) == value

    # ---------- Domain helpers ----------
    def compute_fingerprint(self) -> str:
        base = "|".join([
            _sanitize_for_fingerprint(self.error_type or ""),
            _sanitize_for_fingerprint((self.message or "")[:200]),
            _sanitize_for_fingerprint(self.http_path or ""),
            _sanitize_for_fingerprint(self.error_code or ""),
            _sanitize_for_fingerprint(self.service or ""),
            _sanitize_for_fingerprint(self.component or ""),
        ])
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    def ensure_fingerprint(self) -> None:
        if not self.fingerprint:
            self.fingerprint = self.compute_fingerprint()

    def set_http_context(
        self,
        method: str | None,
        path: str | None,
        *,
        status: int | None = None,
        user_agent: str | None = None,
        client_ip: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        self.http_method = (method or "").upper()[:10] or None
        self.http_path = (path or "")[:400] or None
        self.http_status = status
        self.user_agent = (user_agent or "")[:400] or None
        self.client_ip = (client_ip or "")[:64] or None
        self.latency_ms = None if latency_ms is None else int(latency_ms)

    def set_service_context(
        self,
        *,
        service: str | None = None,
        component: str | None = None,
        release: str | None = None,
        node: str | None = None,
        client_os: str | None = None,
        client_app: str | None = None,
    ) -> None:
        self.service = (service or "")[:80] or None
        self.component = (component or "")[:80] or None
        self.release = (release or "")[:40] or None
        self.node = (node or "")[:80] or None
        self.client_os = (client_os or "")[:80] or None
        self.client_app = (client_app or "")[:80] or None

    def tag(self, **kwargs: str) -> None:
        self.tags = {**(self.tags or {}), **{k: (v if v is None else str(v)) for k, v in kwargs.items()}}

    @classmethod
    def from_exception(
        cls,
        *,
        error_type: str,
        message: str,
        trace: Optional[str] = None,
        severity: ErrorSeverity = ErrorSeverity.error,
        environment: Environment = Environment.unknown,
        source: ErrorSource = ErrorSource.backend,
        user_id: Optional[int] = None,
        http: Optional[Mapping[str, Any]] = None,
        service: Optional[Mapping[str, Any]] = None,
        tags: Optional[Mapping[str, Any]] = None,
        meta: Optional[Mapping[str, Any]] = None,
        occurred_at: Optional[dt.datetime] = None,
    ) -> "ErrorLog":
        e = cls(
            error_type=(error_type or "").strip()[:120],
            message=(message or "").strip(),
            trace=(trace or None),
            severity=severity,
            environment=environment,
            source=source,
            user_id=user_id,
            occurred_at=occurred_at or _utcnow(),
            tags=dict(tags or {}) or None,
            meta=dict(meta or {}) or None,
        )
        if http:
            e.set_http_context(
                method=http.get("method"),
                path=http.get("path"),
                status=http.get("status"),
                user_agent=http.get("ua") or http.get("user_agent"),
                client_ip=http.get("ip") or http.get("client_ip"),
                latency_ms=http.get("latency_ms"),
            )
        if service:
            e.set_service_context(
                service=service.get("service"),
                component=service.get("component"),
                release=service.get("release"),
                node=service.get("node"),
                client_os=service.get("client_os"),
                client_app=service.get("client_app"),
            )
        e.ensure_fingerprint()
        e.minute_bucket = _minute_bucket(e.occurred_at)
        return e

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ErrorLog id={self.id} sev={self.severity} src={self.source} env={self.environment} "
            f"type={self.error_type} status={self.http_status} fp={self.fingerprint and self.fingerprint[:8]}>"
        )

    # ---------- Validations ----------
    @validates("http_status")
    def _validate_status(self, _key: str, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        iv = int(value)
        if not (100 <= iv <= 599):
            raise ValueError("http_status must be between 100 and 599")
        return iv

    @validates("error_type")
    def _validate_type(self, _key: str, value: str) -> str:
        v = (value or "").strip()
        if len(v) < 2:
            raise ValueError("error_type too short")
        return v[:120]

    @validates("error_code")
    def _validate_code(self, _key: str, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip().upper()[:80]

    # ---------- Constraints & Indexes ----------
    __table_args__ = (
        Index("ix_err_user_time", "user_id", "occurred_at"),
        Index("ix_err_source_severity", "source", "severity"),
        Index("ix_err_env_time", "environment", "occurred_at"),
        Index("ix_err_code_status", "error_code", "http_status"),
        Index("ix_err_request", "request_id", "correlation_id"),
        Index("ix_err_fp_minute", "fingerprint", "minute_bucket"),
        Index("ix_err_service_comp", "service", "component"),
        CheckConstraint("length(error_type) >= 2", name="ck_error_type_len"),
        CheckConstraint(
            "http_status IS NULL OR (http_status BETWEEN 100 AND 599)",
            name="ck_error_http_status_range",
        ),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_error_latency_nonneg"),
    )


# ---------- Auto-fill / normalize events ----------
@listens_for(ErrorLog, "before_insert")
def _errorlog_before_insert(_mapper, _connection, target: ErrorLog) -> None:  # pragma: no cover
    target.occurred_at = target.occurred_at or _utcnow()
    target.minute_bucket = _minute_bucket(target.occurred_at)

    if target.http_method:  target.http_method = target.http_method.upper()[:10]
    if target.http_path:    target.http_path = target.http_path[:400]
    if target.user_agent:   target.user_agent = target.user_agent[:400]
    if target.client_ip:    target.client_ip = target.client_ip[:64]
    if target.service:      target.service = target.service[:80]
    if target.component:    target.component = target.component[:80]
    if target.release:      target.release = target.release[:40]
    if target.node:         target.node = target.node[:80]
    if target.client_os:    target.client_os = target.client_os[:80]
    if target.client_app:   target.client_app = target.client_app[:80]

    target.ensure_fingerprint()


@listens_for(ErrorLog, "before_update")
def _errorlog_before_update(_mapper, _connection, target: ErrorLog) -> None:  # pragma: no cover
    if target.occurred_at:
        target.minute_bucket = _minute_bucket(target.occurred_at)

    if target.http_method:  target.http_method = target.http_method.upper()[:10]
    if target.http_path:    target.http_path = target.http_path[:400]
    if target.user_agent:   target.user_agent = target.user_agent[:400]
    if target.client_ip:    target.client_ip = target.client_ip[:64]
    if target.service:      target.service = target.service[:80]
    if target.component:    target.component = target.component[:80]
    if target.release:      target.release = target.release[:40]
    if target.node:         target.node = target.node[:80]
    if target.client_os:    target.client_os = target.client_os[:80]
    if target.client_app:   target.client_app = target.client_app[:80]

    target.ensure_fingerprint()
