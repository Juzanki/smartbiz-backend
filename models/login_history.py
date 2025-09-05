# backend/models/login_history.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import hashlib
import hmac
import os
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


# ---------- Enums ----------
class LoginMethod(str, enum.Enum):
    password   = "password"
    magic_link = "magic_link"
    otp        = "otp"
    oauth      = "oauth"     # Google, Apple, etc.
    sso        = "sso"       # SAML/OIDC enterprise
    api_key    = "api_key"
    refresh    = "refresh"
    unknown    = "unknown"


class LoginPlatform(str, enum.Enum):
    web    = "web"
    mobile = "mobile"
    api    = "api"
    cli    = "cli"
    other  = "other"


class DeviceClass(str, enum.Enum):
    desktop = "desktop"
    mobile  = "mobile"
    tablet  = "tablet"
    bot     = "bot"
    other   = "other"


class LoginStatus(str, enum.Enum):
    success    = "success"
    failed     = "failed"
    challenged = "challenged"   # e.g., MFA required
    throttled  = "throttled"
    blocked    = "blocked"
    locked     = "locked"


# ---------- Model ----------
class LoginHistory(Base):
    """
    Kumbukumbu ya kila jaribio la kuingia (audit-grade):
    - method/platform/status/device
    - MFA (required/passed, na timestamps)
    - risk_score + risk_flags (JSON)
    - geo/context (ip/device/ua) kwa faragha salama (ip_hash)
    - idempotency & correlation fields
    """
    __tablename__ = "login_history"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Nani
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user: Mapped["User"] = relationship(
        "User", back_populates="login_history", passive_deletes=True, lazy="selectin"
    )

    # Jinsi/kunakotoka
    method: Mapped[LoginMethod] = mapped_column(
        SQLEnum(LoginMethod, name="login_method"),
        default=LoginMethod.unknown, nullable=False, index=True
    )
    platform: Mapped[LoginPlatform] = mapped_column(
        SQLEnum(LoginPlatform, name="login_platform"),
        default=LoginPlatform.web, nullable=False, index=True
    )
    device_class: Mapped[DeviceClass] = mapped_column(
        SQLEnum(DeviceClass, name="login_device_class"),
        default=DeviceClass.other, nullable=False, index=True
    )

    # Matokeo & MFA
    status: Mapped[LoginStatus] = mapped_column(
        SQLEnum(LoginStatus, name="login_status"),
        default=LoginStatus.success, nullable=False, index=True
    )
    successful: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mfa_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    mfa_passed:   Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_challenged_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    mfa_verified_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Risk & sababu ya kushindwa
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))  # 0..100
    risk_flags: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    failure_reason: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    error_detail:   Mapped[Optional[str]] = mapped_column(Text)  # verbose (rate limited, wrong password, etc.)

    # Context/Geo (faragha)
    client_ip: Mapped[Optional[str]] = mapped_column(String(64), index=True)    # inaweza kuzimwa kwa sera
    ip_hash:   Mapped[Optional[str]] = mapped_column(String(128), index=True)   # sha256/HMAC(IP)
    user_agent: Mapped[Optional[str]] = mapped_column(String(400))
    device_label: Mapped[Optional[str]] = mapped_column(String(120))  # "Chrome on macOS", "iPhone 15"
    device_fingerprint: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    location: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))  # {"country":"TZ",...}

    # Uhusiano wa session/requests
    session_id:     Mapped[Optional[str]] = mapped_column(String(64), index=True)
    request_id:     Mapped[Optional[str]] = mapped_column(String(64), index=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)

    # Timestamps (alias 'timestamp' kwa backward-compat)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    # -------- Table args: constraints + indices (kama seti moja tu) --------
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_login_idem_key"),
        Index("ix_login_user_created", "user_id", "created_at"),
        Index("ix_login_status_time", "status", "created_at"),
        Index("ix_login_ip_time", "client_ip", "created_at"),
        Index("ix_login_session", "session_id"),
        Index("ix_login_request", "request_id", "correlation_id"),
        Index("ix_login_method_platform", "method", "platform"),
        Index("ix_login_fp_user", "device_fingerprint", "user_id"),
        CheckConstraint("risk_score >= 0 AND risk_score <= 100", name="ck_login_risk_0_100"),
        {"extend_existing": True},
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def timestamp(self) -> dt.datetime:
        """Alias ya nyuma kwa field ya zamani `timestamp`."""
        return self.created_at

    @hybrid_property
    def is_suspicious(self) -> bool:
        """Heuristics ya UI/alerts."""
        if self.status in (LoginStatus.blocked, LoginStatus.locked, LoginStatus.throttled):
            return True
        if self.risk_score >= 70:
            return True
        if self.mfa_required and not self.mfa_passed:
            return True
        return False

    @is_suspicious.expression
    def is_suspicious(cls):
        return func.coalesce(
            func.cast(
                ( (cls.status.in_([LoginStatus.blocked, LoginStatus.locked, LoginStatus.throttled]))
                  | (cls.risk_score >= 70)
                  | (func.coalesce(cls.mfa_required, False) & ~func.coalesce(cls.mfa_passed, False))
                ),
                Integer,
            ),
            0,
        ) == 1

    # ---------- Helpers ----------
    def mark_success(self) -> None:
        self.status = LoginStatus.success
        self.successful = True
        self.failure_reason = None
        self.error_detail = None
        if self.mfa_required and not self.mfa_passed:
            # kama ilihitajika MFA lakini haikupita, hapa tunaweka passed
            self.mfa_passed = True
            self.mfa_verified_at = self.mfa_verified_at or dt.datetime.now(dt.timezone.utc)

    def mark_failed(self, reason: str | None = None, detail: str | None = None) -> None:
        self.status = LoginStatus.failed
        self.successful = False
        self.failure_reason = (reason or "").strip() or None
        self.error_detail = (detail or "").strip() or None

    def require_mfa(self) -> None:
        self.mfa_required = True
        self.status = LoginStatus.challenged
        self.successful = False
        self.mfa_challenged_at = self.mfa_challenged_at or dt.datetime.now(dt.timezone.utc)

    def mfa_ok(self) -> None:
        self.mfa_passed = True
        self.mfa_verified_at = self.mfa_verified_at or dt.datetime.now(dt.timezone.utc)

    def set_ip(self, ip: str | None) -> None:
        """
        Hifadhi hash ya IP kila wakati; `client_ip` itahifadhiwa tu kama env STORE_PLAIN_IP=1.
        Hash hutumia HMAC(IP_HASH_SECRET) kama ipo, vinginevyo sha256(IP).
        """
        ip = (ip or "").strip() or None
        self.client_ip = ip if os.getenv("STORE_PLAIN_IP", "0").lower() in {"1", "true", "yes", "on"} else None
        if ip:
            secret = (os.getenv("IP_HASH_SECRET") or "").encode("utf-8")
            data = ip.encode("utf-8")
            self.ip_hash = hmac.new(secret, data, hashlib.sha256).hexdigest() if secret else hashlib.sha256(data).hexdigest()
        else:
            self.ip_hash = None

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<LoginHistory id={self.id} user={self.user_id} status={self.status} "
            f"method={self.method} platform={self.platform} at={self.created_at}>"
        )


# ---------- Validators / Normalizers ----------
@validates("failure_reason", "device_label", "request_id", "correlation_id", "session_id", "device_fingerprint")
def _trim_short_texts(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    return v or None

@validates("user_agent")
def _limit_user_agent(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    # linda DB dhidi ya UA ndefu mno
    return v[:400] if v else None


# ---------- Event hooks ----------
from sqlalchemy.event import listens_for  # import hapa ili kuepuka dupe top-level

@listens_for(LoginHistory, "before_insert")
def _lh_before_insert(_mapper, _conn, t: LoginHistory) -> None:
    # Normalize
    if t.failure_reason:
        t.failure_reason = t.failure_reason.strip() or None
    if t.user_agent:
        t.user_agent = t.user_agent.strip() or None
    if t.device_label:
        t.device_label = t.device_label.strip() or None
    if t.request_id:
        t.request_id = t.request_id.strip() or None
    if t.correlation_id:
        t.correlation_id = t.correlation_id.strip() or None
    if t.session_id:
        t.session_id = t.session_id.strip() or None
    if t.device_fingerprint:
        t.device_fingerprint = t.device_fingerprint.strip() or None

    # Hakikisha ip_hash ipo endapo client_ip imeletwa lakini sera hairuhusu kuihifadhi wazi
    if (t.client_ip and not t.ip_hash) or (t.client_ip and os.getenv("STORE_PLAIN_IP", "0") not in {"1", "true", "yes", "on"}):
        secret = (os.getenv("IP_HASH_SECRET") or "").encode("utf-8")
        data = t.client_ip.encode("utf-8")
        t.ip_hash = hmac.new(secret, data, hashlib.sha256).hexdigest() if secret else hashlib.sha256(data).hexdigest()
        if os.getenv("STORE_PLAIN_IP", "0").lower() not in {"1", "true", "yes", "on"}:
            t.client_ip = None

@listens_for(LoginHistory, "before_update")
def _lh_before_update(_mapper, _conn, t: LoginHistory) -> None:
    # kama mfa_passed imekuwa True bila timestamp, weka timestamp
    if t.mfa_passed and not t.mfa_verified_at:
        t.mfa_verified_at = dt.datetime.now(dt.timezone.utc)
