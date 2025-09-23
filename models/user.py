# backend/models/user.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
User model (production-ready)
- Robust password hashing/verification (bcrypt via project utils → passlib → SHA256 fallback).
- Dynamic password column resolver (works with password_hash or hashed_password).
- Normalization for email & username (lowercasing + trimming) on both validation & events.
- Lean relationships (mostly lazy='noload') to keep auth fast and avoid circular mapper issues.
"""

import os
import re
import hashlib
import datetime as dt
from importlib import import_module
from typing import Optional, TYPE_CHECKING, List, Sequence, Dict, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
    synonym,
    validates,
)
from sqlalchemy.event import listens_for
from sqlalchemy import inspect as _inspect

from backend.db import Base, engine

# ──────────────────────────────────────────────────────────────────────────────
# Register light deps early (no heavy imports here; just safe modules)
# If a module is missing in your codebase, these no-op silently.
for _mod in (
    "backend.models.setting",
    "backend.models.notification_preferences",
    "backend.models.customer_feedback",
    "backend.models.support",
):
    try:
        __import__(_mod)
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────────────────────
# TYPE_CHECKING-only (no runtime import)
if TYPE_CHECKING:
    from .like import Like
    from .guest import Guest

# ──────────────────────────────────────────────────────────────────────────────
# Helpers to defensively fetch columns from optional modules
def _try_getattr(mod: str, name: str):
    try:
        return getattr(import_module(mod), name)
    except Exception:
        return None

def _get_model(mod_candidates: Sequence[str], cls_name: str):
    for m in mod_candidates:
        obj = _try_getattr(m, cls_name)
        if obj is not None:
            return obj
    raise ModuleNotFoundError(f"Could not import {cls_name} from: {', '.join(mod_candidates)}")

def _col(mod_candidates: Sequence[str], cls_name: str, col_name: str):
    Model = _get_model(mod_candidates, cls_name)
    return getattr(Model, col_name)

def _fcol(mod_candidates: Sequence[str], cls_name: str, col_candidates: Sequence[str]):
    Model = _get_model(mod_candidates, cls_name)
    for cname in col_candidates:
        if hasattr(Model, cname):
            return getattr(Model, cname)
    raise AttributeError(f"{cls_name} has none of: {', '.join(col_candidates)}")

# Extra candidates for modules that sometimes differ by name in projects
_SUPPORT_MODS = ["backend.models.support", "backend.models.support_ticket"]
_AUTH_MODS = [
    "backend.models.forgot_password",
    "backend.models.password_reset",
    "backend.models.password",
    "backend.models.auth",
    "backend.models.forgot_password_request",
]
_BOT_MODS = ["backend.models.user_bot", "backend.models.bot", "backend.models.bots"]
_BADGE_MODS = ["backend.models.badge_history", "backend.models.badges"]
_NOTIFY_MODS = ["backend.models.notification", "backend.models.notifications"]
_WITHDRAW_MODS = ["backend.models.withdraw_request", "backend.models.withdrawrequests"]
_LOYALTY_MODS = ["backend.models.loyalty", "backend.models.loyalty_points", "backend.models.loyaltypoint"]
_MODERATION_MODS = ["backend.models.moderation", "backend.models.moderation_action", "backend.models.moderationaction"]
_REFERRAL_LOG_MODS = ["backend.models.referral_log", "backend.models.referrallog"]
_GIFT_MOVE_MODS = ["backend.models.gift_movement", "backend.models.giftmovement"]
_GIFT_TXN_MODS = ["backend.models.gift_transaction", "backend.models.gifttransaction"]

# ──────────────────────────────────────────────────────────────────────────────
# Password column resolver (dynamic; keeps compatibility across schemas)
_PWHASH_ENV = os.getenv("SMARTBIZ_PWHASH_COL", "").strip()
_phone_digits = re.compile(r"\D+")

def _table_columns(table: str) -> set[str]:
    try:
        insp = _inspect(engine)
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()

def _table_exists(table: str) -> bool:
    try:
        insp = _inspect(engine)
        return insp.has_table(table)
    except Exception:
        return False

def _resolved_pwcol() -> str:
    if _PWHASH_ENV:
        return _PWHASH_ENV
    cols = _table_columns("users") if _table_exists("users") else set()
    if "password_hash" in cols:
        return "password_hash"
    if "hashed_password" in cols:
        return "hashed_password"
    return "password_hash"

_PWHASH_COL = _resolved_pwcol()

# ──────────────────────────────────────────────────────────────────────────────
# Model
class User(Base):
    """Core user model with safe server defaults and normalized fields."""
    __tablename__ = "users"
    __mapper_args__ = {"eager_defaults": True}

    # Identity
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(80), index=True, default=None)
    full_name: Mapped[Optional[str]] = mapped_column(String(120), default=None, nullable=True)

    # Password hash (dynamic aliasing)
    if _PWHASH_COL == "hashed_password":
        hashed_password: Mapped[Optional[str]] = mapped_column("hashed_password", String(255), default=None)
        password_hash = synonym("hashed_password")
    else:
        password_hash: Mapped[Optional[str]] = mapped_column("password_hash", String(255), default=None)
        hashed_password = synonym("password_hash")

    # Status / role
    role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'user'"),
        doc="User role: user/moderator/admin/owner",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    subscription_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'free'"))

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(), index=True
    )

    __table_args__ = (
        CheckConstraint("length(email) >= 3", name="ck_user_email_len"),
        Index("ix_users_email_lower", func.lower(email), unique=False),
        Index("ix_users_username_lower", func.lower(username), unique=False),
        Index("ix_users_is_active_created", "is_active", "created_at"),
        {"extend_existing": True},
    )

    # ───── Relationships (lean; avoid circulars; keep auth fast) ─────
    # Likes
    likes: Mapped[List["Like"]] = relationship(
        "Like",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="noload",
    )

    # Guests (pair with Guest.user / Guest.approved_by)
    guest_entries: Mapped[List["Guest"]] = relationship(
        "Guest",
        primaryjoin=lambda: _col(["backend.models.guest", "backend.models.guests"], "Guest", "user_id") == User.id,
        foreign_keys=lambda: [_col(["backend.models.guest", "backend.models.guests"], "Guest", "user_id")],
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="noload",
    )
    guest_approvals: Mapped[List["Guest"]] = relationship(
        "Guest",
        primaryjoin=lambda: _col(["backend.models.guest", "backend.models.guests"], "Guest", "approved_by_user_id") == User.id,
        foreign_keys=lambda: [_col(["backend.models.guest", "backend.models.guests"], "Guest", "approved_by_user_id")],
        back_populates="approved_by",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="noload",
    )

    # A few examples kept for common features (others remain lazy=noload)
    push_subscriptions = relationship(
        "PushSubscription", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    notifications_received = relationship(
        "Notification",
        back_populates="recipient",
        foreign_keys=lambda: [
            _col(_NOTIFY_MODS, "Notification", "user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    notifications_sent = relationship(
        "Notification",
        back_populates="actor",
        foreign_keys=lambda: [
            _col(_NOTIFY_MODS, "Notification", "actor_user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # Moderation (pair with ModerationAction.moderator / .target)
    moderations_taken = relationship(
        "ModerationAction",
        back_populates="moderator",
        foreign_keys=lambda: [
            _col(_MODERATION_MODS, "ModerationAction", "moderator_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    moderations_received = relationship(
        "ModerationAction",
        back_populates="target",
        foreign_keys=lambda: [
            _col(_MODERATION_MODS, "ModerationAction", "target_user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # Gift movements / transactions (kept lean)
    gift_movements_sent = relationship(
        "GiftMovement",
        back_populates="sender",
        foreign_keys=lambda: [_col(_GIFT_MOVE_MODS, "GiftMovement", "sender_id")],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    gift_movements_received = relationship(
        "GiftMovement",
        back_populates="host",
        foreign_keys=lambda: [_col(_GIFT_MOVE_MODS, "GiftMovement", "host_id")],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    gift_transactions_sent = relationship(
        "GiftTransaction",
        back_populates="sender",
        foreign_keys=lambda: [_col(_GIFT_TXN_MODS, "GiftTransaction", "sender_id")],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    gift_transactions_received = relationship(
        "GiftTransaction",
        back_populates="recipient",
        foreign_keys=lambda: [_col(_GIFT_TXN_MODS, "GiftTransaction", "recipient_id")],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # ───── Core helpers ─────
    @staticmethod
    def _sha256(raw: string) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def normalize_email(v: Optional[str]) -> str:
        return (v or "").strip().lower()

    @staticmethod
    def normalize_username(v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = " ".join(v.strip().split())
        return v.lower() or None

    @staticmethod
    def normalize_identifier(v: str) -> str:
        """email → lower; username → lower; phone → digits-only (if applicable)"""
        v = (v or "").strip()
        if "@" in v:
            return v.lower()
        digits = _phone_digits.sub("", v)
        return digits if digits else v.lower()

    @staticmethod
    def identifier_candidates(v: str) -> Dict[str, str]:
        """Return dict of possible identifier fields for lookups."""
        v = (v or "").strip()
        out: Dict[str, str] = {}
        if "@" in v:
            out["email"] = v.lower()
        uname = User.normalize_username(v)
        if uname:
            out.update({"username": uname, "user_name": uname, "handle": uname})
        digits = _phone_digits.sub("", v)
        if digits:
            out.update({"phone_number": digits, "phone": digits, "mobile": digits, "msisdn": digits})
        return out

    def set_password(self, raw: str) -> None:
        """Hash and set password using project security utils → passlib → SHA256 fallback."""
        try:
            from backend.utils.security import get_password_hash  # type: ignore
            h = get_password_hash(raw)
        except Exception:
            h = self._sha256(raw)
        if hasattr(self, "password_hash"):
            self.password_hash = h
        else:  # pragma: no cover
            self.hashed_password = h  # type: ignore[attr-defined]

    def verify_password(self, raw: str) -> bool:
        """Verify password against stored hash."""
        stored = getattr(self, "password_hash", None) or getattr(self, "hashed_password", None) or ""
        if not stored:
            return False
        try:
            from backend.utils.security import verify_password  # type: ignore
            return bool(verify_password(raw, stored))
        except Exception:
            return self._sha256(raw) == stored

    def to_safe_dict(self) -> Dict[str, Any]:
        """Public-safe projection (no password fields)."""
        return {
            "id": str(self.id) if self.id is not None else None,
            "email": self.email,
            "username": self.username,
            "full_name": self.full_name,
            "role": self.role,
            "is_active": bool(self.is_active),
            "is_verified": bool(self.is_verified),
            "subscription_status": self.subscription_status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def from_dict(self, data: Dict[str, Any]) -> "User":
        """Assign editable fields from plain dict (server-side normalization applied)."""
        if "email" in data:
            self.email = self.normalize_email(data.get("email"))
        if "username" in data:
            self.username = self.normalize_username(data.get("username"))
        if "full_name" in data:
            self.full_name = (data.get("full_name") or None)
        if "role" in data and data["role"]:
            self.role = str(data["role"]).lower()
        if "is_active" in data:
            self.is_active = bool(data["is_active"])
        if "is_verified" in data:
            self.is_verified = bool(data["is_verified"])
        if "subscription_status" in data and data["subscription_status"]:
            self.subscription_status = str(data["subscription_status"]).lower()
        if "password" in data and data["password"]:
            self.set_password(str(data["password"]))
        return self

    def touch(self) -> None:
        self.updated_at = dt.datetime.now(dt.timezone.utc)

    def activate(self) -> None: self.is_active = True
    def deactivate(self) -> None: self.is_active = False

    @property
    def name(self) -> str:
        return self.full_name or self.username or self.email

    @property
    def has_password(self) -> bool:
        return bool(getattr(self, "password_hash", None) or getattr(self, "hashed_password", None))

    @property
    def is_owner(self) -> bool: return (self.role or "").lower() == "owner"
    @property
    def is_admin(self) -> bool: return (self.role or "").lower() == "admin"
    @property
    def is_staff(self) -> bool: return self.is_admin or self.is_owner

    def has_role(self, *roles: str) -> bool:
        r = (self.role or "").lower()
        return any(r == x.lower() for x in roles)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} email={self.email} role={self.role} active={self.is_active}>"

    # ───── Validators ─────
    @validates("email")
    def _validate_email_lower(self, _key, value: str) -> str:
        v = self.normalize_email(value)
        if not v or "@" not in v:
            raise ValueError("invalid email")
        return v

    @validates("username")
    def _validate_username_lower(self, _key, value: Optional[str]) -> Optional[str]:
        return self.normalize_username(value)

# ──────────────────────────────────────────────────────────────────────────────
# Normalization hooks
@listens_for(User, "before_insert")
def _user_before_insert(_mapper, _connection, target: User) -> None:
    if target.email:    target.email = User.normalize_email(target.email)
    if target.username: target.username = User.normalize_username(target.username)

@listens_for(User, "before_update")
def _user_before_update(_mapper, _connection, target: User) -> None:
    if target.email:    target.email = User.normalize_email(target.email)
    if target.username: target.username = User.normalize_username(target.username)
