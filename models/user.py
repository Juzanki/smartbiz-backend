# backend/models/user.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import hashlib
import datetime as dt
import re
from contextlib import suppress
from typing import Optional, Dict, Any

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Index, Integer, String, func, text
)
from sqlalchemy.orm import Mapped, mapped_column, synonym, validates
from sqlalchemy import inspect as _sa_inspect

# Chukua Base/engine kutoka db.py (iwe layouts zote)
try:
    from backend.db import Base, engine  # type: ignore
except Exception:  # pragma: no cover
    from db import Base, engine  # type: ignore

# ──────────────────────────────────────────────────────
# Chagua jina la safu ya password kulingana na ENV au DB
# ──────────────────────────────────────────────────────
_PW_ENV = (os.getenv("SMARTBIZ_PWHASH_COL") or "").strip().lower()
_digits = re.compile(r"\D+")

def _detect_pw_col() -> str:
    # 1) Kipaumbele: ENV
    if _PW_ENV in {"password_hash", "hashed_password", "password"}:
        return _PW_ENV
    # 2) Jaribu kusoma kolamu zilizopo
    with suppress(Exception):
        insp = _sa_inspect(engine)
        if insp.has_table("users"):
            cols = {c["name"] for c in insp.get_columns("users")}
            for name in ("hashed_password", "password_hash", "password"):
                if name in cols:
                    return name
    # 3) Chaguo-msingi ukijenga jedwali jipya
    return "password_hash"

_PW_COL = _detect_pw_col()

# ──────────────────────────────────────────────────────
# Msaada wa hashing/verification (project utils → passlib → SHA256)
# ──────────────────────────────────────────────────────
def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _hash_password(raw: str) -> str:
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
    try:
        from backend.utils.security import verify_password  # type: ignore
        return bool(verify_password(raw, stored))
    except Exception:
        try:
            from utils.security import verify_password  # type: ignore
            return bool(verify_password(raw, stored))
        except Exception:
            return _sha256(raw) == (stored or "")

# ──────────────────────────────────────────────────────
# Model
# ──────────────────────────────────────────────────────
class User(Base):
    """
    Model nyepesi ya mtumiaji:
      - Inanormalize email/username (lowercase, trim).
      - Inatumia password column yoyote: password_hash / hashed_password / password.
      - Inakuwa na synonyms ili access ya `password_hash` *au* `hashed_password` isabike popote.
    """
    __tablename__ = "users"
    __mapper_args__ = {"eager_defaults": True}

    # Vitambulisho
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, unique=True, index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    # Safu ya password (hujengwa kulingana na _PW_COL)
    if _PW_COL == "hashed_password":
        hashed_password: Mapped[Optional[str]] = mapped_column("hashed_password", String(255), nullable=True)
        password_hash = synonym("hashed_password")  # kuwezesha code nyingine kuitumia
    elif _PW_COL == "password":
        password: Mapped[Optional[str]] = mapped_column("password", String(255), nullable=True)
        # synonyms mbili zi-reflect kolamu moja
        password_hash = synonym("password")
        hashed_password = synonym("password")
    else:
        # default: password_hash
        password_hash: Mapped[Optional[str]] = mapped_column("password_hash", String(255), nullable=True)
        hashed_password = synonym("password_hash")

    # Hali/nafasi
    role: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'user'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # Nyakati
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                    server_default=func.now(), index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                    server_default=func.now(), onupdate=func.now(), index=True)

    __table_args__ = (
        CheckConstraint("length(email) >= 3", name="ck_user_email_len"),
        # Functional indexes (zinasaidia utafutaji case-insensitive)
        Index("ix_users_email_lower", func.lower(email), unique=False),
        Index("ix_users_username_lower", func.lower(username), unique=False),
        {"extend_existing": True},
    )

    # ───── Helpers ─────
    @staticmethod
    def normalize_email(v: Optional[str]) -> str:
        return (v or "").strip().lower()

    @staticmethod
    def normalize_username(v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = " ".join(v.strip().split()).lower()
        return v or None

    @staticmethod
    def normalize_identifier(v: str) -> str:
        v = (v or "").strip()
        if "@" in v:
            return v.lower()
        return v.lower()

    def set_password(self, raw: str) -> None:
        h = _hash_password(raw)
        # weka kwenye property iliyo “live” (synonyms hushughulikia upande mwingine)
        if hasattr(self, "password_hash"):
            setattr(self, "password_hash", h)
        else:
            setattr(self, "hashed_password", h)

    def verify_password(self, raw: str) -> bool:
        stored = (
            getattr(self, "password_hash", None)
            or getattr(self, "hashed_password", None)
            or getattr(self, "password", None)
            or ""
        )
        return _verify_password(raw, stored)

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": int(self.id) if self.id is not None else None,
            "email": self.email,
            "username": self.username,
            "full_name": self.full_name,
            "role": self.role,
            "is_active": bool(self.is_active),
            "is_verified": bool(self.is_verified),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    # ───── Validators (SQLAlchemy) ─────
    @validates("email")
    def _validate_email(self, _k, v: str) -> str:
        v = self.normalize_email(v)
        if not v or "@" not in v:
            raise ValueError("invalid email")
        return v

    @validates("username")
    def _validate_username(self, _k, v: Optional[str]) -> Optional[str]:
        return self.normalize_username(v)

    # ───── Utilities ─────
    @property
    def name(self) -> str:
        return self.full_name or self.username or self.email

    @property
    def has_password(self) -> bool:
        return bool(
            getattr(self, "password_hash", None)
            or getattr(self, "hashed_password", None)
            or getattr(self, "password", None)
        )

    def touch(self) -> None:
        self.updated_at = dt.datetime.now(dt.timezone.utc)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} email={self.email} active={self.is_active}>"

# ───── Normalize kabla ya kuandika/kuhuisha ─────
from sqlalchemy.event import listens_for

@listens_for(User, "before_insert")
def _before_insert(_mapper, _conn, target: User) -> None:
    if target.email:
        target.email = User.normalize_email(target.email)
    if target.username:
        target.username = User.normalize_username(target.username)

@listens_for(User, "before_update")
def _before_update(_mapper, _conn, target: User) -> None:
    if target.email:
        target.email = User.normalize_email(target.email)
    if target.username:
        target.username = User.normalize_username(target.username)

__all__ = ["User"]
