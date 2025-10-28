# backend/models/user.py
# -*- coding: utf-8 -*-
"""
SmartBiz Assistance — User model
ULTRA-STABLE (Render-safe) v3

Why this hardened version exists:

On Render, if ANY mapper fails to initialize, the whole app dies.
A mapper will fail if:
  - there's a circular import chain, OR
  - another model declares back_populates="X" but this User model
    doesn't actually define attribute X.

We have (today) other models that point back to User like this:
  - AIBotSettings.user                -> back_populates="ai_bot_settings"
  - ForgotPasswordRequest.user        -> back_populates="forgot_password_requests"
  - PasswordResetCode.user            -> back_populates="password_resets"

If we don't define *all three* of those lists on User, SQLAlchemy explodes
during app startup on Render.

Strategy:
- Keep User lean, predictable, and circular-import safe.
- Only declare REQUIRED relationships (the ones other models already expect).
- Do NOT add heavy cross-model webs (payments, wallets, livestreams, etc.)
  unless you're 100% sure they won't recurse-import User again.

Also included:
- UUID PK (Postgres UUID)
- email / phone / profile metadata
- password hashing with fallback if security utils aren't importable yet
- activity timestamps
- GDPR-ish anonymize helper
- before_insert / before_update listeners for normalization
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import os
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional, List

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Numeric,
    String,
    text,
    func,
)
from sqlalchemy.dialects.postgresql import INET, UUID as PGUUID
from sqlalchemy.event import listens_for
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
    synonym,
    validates,
)

# ------------------------------------------------------------------------------
# Base import with fallback (Render vs local dev)
# ------------------------------------------------------------------------------
try:
    from backend.db import Base  # type: ignore
except Exception:  # pragma: no cover
    from db import Base  # type: no cover  # type: ignore


# ------------------------------------------------------------------------------
# Password helpers
# ------------------------------------------------------------------------------
def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_password(raw: str) -> str:
    """
    Preferred: backend.utils.security.get_password_hash (bcrypt/argon2/etc).
    Fallback: sha256 so this file can stand alone, even if security utils
    can't import during partial boot.
    """
    try:
        from backend.utils.security import get_password_hash  # type: ignore
        return get_password_hash(raw)
    except Exception:
        try:
            from utils.security import get_password_hash  # type: ignore
            return get_password_hash(raw)
        except Exception:
            return _sha256(raw)


def _verify_password(raw: str, stored: str) -> bool:
    """
    Preferred: backend.utils.security.verify_password.
    Fallback: sha256 equality so auth checks won't explode in minimal mode.
    """
    try:
        from backend.utils.security import verify_password  # type: ignore
        return bool(verify_password(raw, stored))
    except Exception:
        try:
            from utils.security import verify_password  # type: ignore
            return bool(verify_password(raw, stored))
        except Exception:
            return _sha256(raw) == (stored or "")


# Which DB column actually stores the password hash?
# We'll expose .password_hash regardless of which real column is in use.
_PW_ENV = (os.getenv("SMARTBIZ_PWHASH_COL") or "").strip().lower()
if _PW_ENV not in {"password_hash", "hashed_password", "password"}:
    _PW_ENV = "password_hash"


# ------------------------------------------------------------------------------
# Model
# ------------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        CheckConstraint(
            "length(trim(coalesce(email,''))) > 0",
            name="email_not_empty",
            info={"no_autogenerate": True},
        ),
        {"extend_existing": True},
    )

    # -- identity / auth -------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        index=True,
        default=uuid.uuid4,
    )

    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        doc="Login / notification email (stored normalized lowercase).",
    )

    username: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        doc="Public handle / vanity name.",
    )

    full_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    bio: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    phone: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    phone_country: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        server_default=text("'TZ'"),
        doc="Country code for phone e.g. TZ, KE, US.",
    )

    role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'user'"),
        doc="Global app role (user/mod/admin/owner). NOT org-scoped.",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )

    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        doc="Account verified? (email / phone / KYC).",
    )

    # -- password material -----------------------------------------------------
    # We alias to .password_hash no matter what env config says.
    if _PW_ENV == "hashed_password":
        hashed_password: Mapped[str] = mapped_column(
            "hashed_password",
            String,
            nullable=False,
        )
        password_hash = synonym("hashed_password")
    elif _PW_ENV == "password":
        password: Mapped[str] = mapped_column(
            "password",
            String,
            nullable=False,
        )
        password_hash = synonym("password")        # type: ignore
        hashed_password = synonym("password")      # type: ignore
    else:
        password_hash: Mapped[str] = mapped_column(
            "password_hash",
            String,
            nullable=False,
        )
        hashed_password = synonym("password_hash")  # type: ignore

    preferred_language: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        server_default=text("'en'"),
    )

    business_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    business_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    ad_earnings: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        server_default=text("0"),
        doc="Cumulative ad revenue / creator payouts snapshot.",
    )

    # -- timestamps & status ---------------------------------------------------
    last_login_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        index=True,
    )

    anonymized_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    terms_accepted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    pii_erasure_note: Mapped[Optional[str]] = mapped_column(String)

    signup_ip: Mapped[Optional[str]] = mapped_column(INET)
    signup_user_agent: Mapped[Optional[str]] = mapped_column(String)

    # ------------------------------------------------------------------------------
    # MINIMAL, REQUIRED RELATIONSHIPS
    # These MUST exist because other models refer to them with back_populates.
    # If you remove any of these, Render will crash on boot.
    # ------------------------------------------------------------------------------

    # AIBotSettings.user -> back_populates="ai_bot_settings"
    ai_bot_settings: Mapped[List["AIBotSettings"]] = relationship(
        "backend.models.ai_bot_settings.AIBotSettings",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )

    # ForgotPasswordRequest.user -> back_populates="forgot_password_requests"
    forgot_password_requests: Mapped[List["ForgotPasswordRequest"]] = relationship(
        "backend.models.forgot_password.ForgotPasswordRequest",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )

    # PasswordResetCode.user -> back_populates="password_resets"
    password_resets: Mapped[List["PasswordResetCode"]] = relationship(
        "backend.models.password_reset_code.PasswordResetCode",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )

    # ------------------------------------------------------------------------------
    # Instance helpers
    # ------------------------------------------------------------------------------
    @staticmethod
    def _now() -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    @staticmethod
    def normalize_email(v: Optional[str]) -> str:
        return (v or "").strip().lower()

    @staticmethod
    def normalize_username(v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = " ".join(v.strip().split()).lower()
        return v or None

    def set_password(self, raw: str) -> None:
        """Hash and assign a new password to this user."""
        self.password_hash = _hash_password(raw)

    def verify_password(self, raw: str) -> bool:
        return _verify_password(raw, self.password_hash or "")

    def mark_verified(self) -> None:
        self.is_verified = True

    def deactivate(self, *, soft_delete: bool = False) -> None:
        """
        Disable account. Optionally mark deleted_at as soft delete.
        """
        self.is_active = False
        if soft_delete and not self.deleted_at:
            self.deleted_at = self._now()

    def touch_last_login(self) -> None:
        self.last_login_at = self._now()

    def display_name(self) -> str:
        """
        Best label for UI headers / chat bubbles.
        Priority: full_name -> username -> email
        """
        return (self.full_name or self.username or self.email or "").strip()

    def soft_anonymize(self, *, note: Optional[str] = None) -> None:
        """
        Scrub direct PII but keep row for analytics / fraud / payout / audit.
        Leaves a trail via pii_erasure_note + anonymized_at.
        """
        self.full_name = None
        self.avatar_url = None
        self.bio = None
        self.phone = None
        self.signup_ip = None
        self.signup_user_agent = None
        self.pii_erasure_note = note or "anonymized"
        self.anonymized_at = self._now()

    def to_safe_dict(self) -> Dict[str, Any]:
        """
        Public-safe data for API responses.
        NOTE: DOES NOT expose password_hash or secret fields.
        """
        return {
            "id": str(self.id) if self.id else None,
            "email": self.email,
            "username": self.username,
            "full_name": self.full_name,
            "avatar_url": self.avatar_url,
            "bio": self.bio,
            "role": self.role,
            "is_active": bool(self.is_active),
            "is_verified": bool(self.is_verified),
            "phone": self.phone,
            "phone_country": self.phone_country,
            "preferred_language": self.preferred_language,
            "business_name": self.business_name,
            "business_type": self.business_type,
            "ad_earnings": (
                str(self.ad_earnings) if self.ad_earnings is not None else "0.00"
            ),
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    # ------------------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------------------
    @validates("email")
    def _v_email(self, _k, v: str) -> str:
        v = User.normalize_email(v)
        if not v or "@" not in v:
            raise ValueError("invalid email")
        return v

    @validates("username")
    def _v_username(self, _k, v: Optional[str]) -> Optional[str]:
        return User.normalize_username(v)

    @validates("signup_ip")
    def _v_signup_ip(self, _k, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        try:
            ipaddress.ip_address(v)
        except Exception:
            return None
        return v

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} email={self.email!r} role={self.role} active={self.is_active}>"


# ------------------------------------------------------------------------------
# ORM LISTENERS
# We attach to Base with propagate=True so all mapped subclasses fire,
# but we guard with isinstance(target, User) to avoid touching other models.
# ------------------------------------------------------------------------------

@listens_for(Base, "before_insert", propagate=True)
def _on_user_insert(mapper, conn, target):  # type: ignore[no-redef]
    """
    Normalize + default fields right before INSERT.
    Keeps DB consistent even if upstream forgets to sanitize.
    """
    if not isinstance(target, User):
        return

    # normalize text fields
    if target.email:
        target.email = User.normalize_email(target.email)
    if target.username:
        target.username = User.normalize_username(target.username)

    # safety defaults
    if not target.role:
        target.role = "user"

    # timestamps fallback
    now = User._now()
    if not target.created_at:
        target.created_at = now
    if not target.updated_at:
        target.updated_at = now


@listens_for(Base, "before_update", propagate=True)
def _on_user_update(mapper, conn, target):  # type: ignore[no-redef]
    """
    Normalize + bump updated_at on UPDATE.
    """
    if not isinstance(target, User):
        return

    if target.email:
        target.email = User.normalize_email(target.email)
    if target.username:
        target.username = User.normalize_username(target.username)

    target.updated_at = User._now()
