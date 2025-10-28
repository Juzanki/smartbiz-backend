# backend/models/user.py
# -*- coding: utf-8 -*-
"""
SmartBiz Assistance — User model
ULTRA-STABLE (Render-safe) v4

WHY THIS FILE EXISTS
--------------------
On Render, if ANY SQLAlchemy mapper fails to initialize, the entire app
refuses to boot. A mapper can fail if:

  1. There's a circular import chain that prevents some model class from
     being fully defined before SQLAlchemy tries to configure mappers.

  2. Another model says back_populates="something", but THIS User model
     doesn't actually define attribute "something". Example crashes we've hit:

        AIBotSettings.user
            -> back_populates="ai_bot_settings"
        ForgotPasswordRequest.user
            -> back_populates="forgot_password_requests"
        PasswordResetCode.user
            -> back_populates="password_resets"

     If User is missing ANY of those attributes, mapper init dies on boot.

GOAL / STRATEGY
---------------
- Keep this model lean and predictable.
- Explicitly declare ONLY the relationships that other models currently
  REQUIRE via back_populates. (Do NOT import those models directly; we
  reference them using fully-qualified dotted path strings to avoid
  circular imports.)
- Avoid declaring heavy optional relationships (payments, wallets,
  livestream guests, etc.) unless we're sure they are safe.

Currently guaranteed required:
    - ai_bot_settings            <-> AIBotSettings.user
    - forgot_password_requests   <-> ForgotPasswordRequest.user
    - password_resets            <-> PasswordResetCode.user

NOTE:
We will probably ALSO need to add in future:
    - gift_movements_sent        <-> GiftMovement.sender
    - gift_movements_received    <-> GiftMovement.host
BUT those aren't declared yet unless gift_movement.py expects them.
If/when gift_movement.py uses back_populates="gift_movements_sent"/
"gift_movements_received", we MUST add them here or Render will crash.

OTHER THINGS WE KEEP
--------------------
- UUID primary key
- auth / profile metadata
- password hashing (with fallback if security utils can't import yet)
- timestamps, soft-delete, GDPR-style anonymize
- before_insert / before_update listeners that normalize data
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
    from db import Base  # type: ignore


# ------------------------------------------------------------------------------
# Password helpers
# ------------------------------------------------------------------------------
def _sha256(raw: str) -> str:
    """SHA256 fallback so we never fail hard if bcrypt libs can't import."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_password(raw: str) -> str:
    """
    Preferred path: backend.utils.security.get_password_hash (bcrypt/argon2/etc).
    Fallback: SHA256 so this file can still run in "bootstrap mode" even if
    security utilities aren't available yet.
    """
    try:
        from backend.utils.security import get_password_hash  # type: ignore
        return get_password_hash(raw)
    except Exception:
        try:
            from utils.security import get_password_hash  # type: ignore
            return get_password_hash(raw)
        except Exception:
            # last-resort fallback
            return _sha256(raw)


def _verify_password(raw: str, stored: str) -> bool:
    """
    Preferred path: backend.utils.security.verify_password.
    Fallback: constant-time-ish SHA256 equality.
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
# We ALWAYS expose .password_hash on the instance either way.
_PW_ENV = (os.getenv("SMARTBIZ_PWHASH_COL") or "").strip().lower()
if _PW_ENV not in {"password_hash", "hashed_password", "password"}:
    _PW_ENV = "password_hash"


# ------------------------------------------------------------------------------
# Main model
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

    # -- Identity / auth -------------------------------------------------------
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
        doc="Login / notification email. Stored normalized (lowercase+trim).",
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
        doc="Phone country code e.g. TZ, KE, US.",
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

    # -- Password material -----------------------------------------------------
    # We always want a .password_hash attribute on the instance regardless of
    # what the underlying column is called in the DB. That keeps the rest of
    # the codebase simple.
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
    last_login_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True)
    )

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
    terms_accepted_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True)
    )

    pii_erasure_note: Mapped[Optional[str]] = mapped_column(String)

    signup_ip: Mapped[Optional[str]] = mapped_column(INET)
    signup_user_agent: Mapped[Optional[str]] = mapped_column(String)

    # ------------------------------------------------------------------------------
    # REQUIRED RELATIONSHIPS
    #
    # These MUST exist because other models define back_populates expecting
    # these names. If you remove/rename them, Render will FAIL TO BOOT.
    #
    # Use fully-qualified dotted model paths so SQLAlchemy can lazy-resolve the
    # class without importing it immediately. This avoids circular import loops.
    # ------------------------------------------------------------------------------

    # AIBotSettings.user  -> back_populates="ai_bot_settings"
    ai_bot_settings: Mapped[List["AIBotSettings"]] = relationship(
        "backend.models.ai_bot_settings.AIBotSettings",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # ForgotPasswordRequest.user -> back_populates="forgot_password_requests"
    forgot_password_requests: Mapped[List["ForgotPasswordRequest"]] = relationship(
        "backend.models.forgot_password.ForgotPasswordRequest",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # PasswordResetCode.user -> back_populates="password_resets"
    password_resets: Mapped[List["PasswordResetCode"]] = relationship(
        "backend.models.password_reset_code.PasswordResetCode",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # NOTE (future):
    # If GiftMovement.sender has `back_populates="gift_movements_sent"`
    # and GiftMovement.host   has `back_populates="gift_movements_received"`
    # we MUST add:
    #
    # gift_movements_sent: Mapped[List["GiftMovement"]] = relationship(
    #     "backend.models.gift_movement.GiftMovement",
    #     back_populates="sender",
    #     foreign_keys="backend.models.gift_movement.GiftMovement.sender_id",
    #     passive_deletes=True,
    #     lazy="selectin",
    # )
    #
    # gift_movements_received: Mapped[List["GiftMovement"]] = relationship(
    #     "backend.models.gift_movement.GiftMovement",
    #     back_populates="host",
    #     foreign_keys="backend.models.gift_movement.GiftMovement.host_id",
    #     passive_deletes=True,
    #     lazy="selectin",
    # )
    #
    # If we forget to add them after updating gift_movement.py, Render will
    # hard crash during startup.

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
        Disable account. Optionally stamp deleted_at for soft-deletion.
        """
        self.is_active = False
        if soft_delete and not self.deleted_at:
            self.deleted_at = self._now()

    def touch_last_login(self) -> None:
        """Update last_login_at to now()."""
        self.last_login_at = self._now()

    def display_name(self) -> str:
        """
        Best label for UI/chat bubbles:
        priority full_name -> username -> email.
        """
        return (self.full_name or self.username or self.email or "").strip()

    def soft_anonymize(self, *, note: Optional[str] = None) -> None:
        """
        Scrub direct PII but keep row for analytics/fraud/payout/audit.
        Leaves a trace for audit via pii_erasure_note + anonymized_at.
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
        Public-safe profile data for API responses.
        DOES NOT expose password_hash.
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
            "last_login_at": (
                self.last_login_at.isoformat() if self.last_login_at else None
            ),
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
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
        return (
            f"<User id={self.id} "
            f"email={self.email!r} "
            f"role={self.role!r} "
            f"active={self.is_active}>"
        )


# ------------------------------------------------------------------------------
# ORM LISTENERS
# We attach to Base with propagate=True so all mapped subclasses fire,
# but we guard with isinstance(target, User) to only touch User rows.
# ------------------------------------------------------------------------------

@listens_for(Base, "before_insert", propagate=True)
def _on_user_insert(mapper, conn, target):  # type: ignore[no-redef]
    """
    Normalize + default fields right before INSERT.

    WHY:
    - Keeps DB consistent even if upstream/routers forget to sanitize data.
    - Works as a last line of defense in production and during migrations.
    """
    if not isinstance(target, User):
        return

    if target.email:
        target.email = User.normalize_email(target.email)
    if target.username:
        target.username = User.normalize_username(target.username)

    if not target.role:
        target.role = "user"

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

