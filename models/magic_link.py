# backend/models/magic_link.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import os
import secrets
import hashlib
import hmac
import datetime as dt
from typing import Optional, Sequence, TYPE_CHECKING, Dict, Any, List

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
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
class MagicLinkStatus(str, enum.Enum):
    pending  = "pending"
    used     = "used"
    revoked  = "revoked"
    expired  = "expired"


# ---------- Time helpers ----------
def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------- Token hashing ----------
def _pepper() -> bytes:
    return (os.getenv("MAGIC_LINK_PEPPER") or "").encode("utf-8")

def _hash_token(plain: str) -> str:
    # token_hash = sha256(pepper || token)
    p = _pepper()
    data = (p + (plain or "").encode("utf-8"))
    return hashlib.sha256(data).hexdigest()


class MagicLink(Base):
    """
    Magic link salama (audit-grade):
    - Tunahifadhi **HASH ya token tu** (si plaintext); verify kwa compare_digest.
    - Vikwazo vya IP/UA/allowlist (hiari).
    - max_uses (chaguo-msingi 1) na used_count; status lifecycle.
    - Timestamps + helpers za consumption/revoke/extend.
    """
    __tablename__ = "magic_links"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_magic_token_hash"),
        UniqueConstraint("idempotency_key", name="uq_magic_idem_key"),
        Index("ix_magic_user_created", "user_id", "created_at"),
        Index("ix_magic_status_expiry", "status", "expires_at"),
        Index("ix_magic_used", "used"),
        Index("ix_magic_ip", "lock_ip"),
        Index("ix_magic_request", "request_id"),
        # Guards
        CheckConstraint("max_uses >= 1", name="ck_magic_max_uses_min1"),
        CheckConstraint("used_count >= 0", name="ck_magic_used_count_nonneg"),
        CheckConstraint("expires_at > created_at", name="ck_magic_expiry_after_create"),
        {"extend_existing": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Owner
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user: Mapped["User"] = relationship(
        "User", back_populates="magic_links", passive_deletes=True, lazy="selectin"
    )

    # Secret (hash-only)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    token_hint: Mapped[Optional[str]] = mapped_column(String(12))  # chars za mwanzo kwa logs/trace

    # Restrictions / context
    lock_ip: Mapped[Optional[str]] = mapped_column(String(64))
    ip_allowlist: Mapped[Optional[List[str]]] = mapped_column(as_mutable_json(JSON_VARIANT))  # ["1.2.3.4", ...]
    ua_substring: Mapped[Optional[str]] = mapped_column(String(160))
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Usage controls
    max_uses:  Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # Lifecycle
    status: Mapped[MagicLinkStatus] = mapped_column(
        SQLEnum(MagicLinkStatus, name="magic_link_status"),
        default=MagicLinkStatus.pending,
        nullable=False,
        index=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    # Consumption context
    consumed_ip: Mapped[Optional[str]] = mapped_column(String(64))
    consumed_ua: Mapped[Optional[str]] = mapped_column(String(400))
    request_id:  Mapped[Optional[str]] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)

    # ---------- Hybrids ----------
    @hybrid_property
    def is_expired(self) -> bool:
        return bool(self.expires_at and _utcnow() >= self.expires_at)

    @hybrid_property
    def remaining_uses(self) -> int:
        return max(0, (self.max_uses or 1) - (self.used_count or 0))

    @hybrid_property
    def consumable(self) -> bool:
        return (self.status == MagicLinkStatus.pending
                and not self.is_expired
                and self.remaining_uses > 0)

    # ---------- Token helpers ----------
    @classmethod
    def hash_token(cls, plain: str) -> str:
        return _hash_token(plain)

    def set_token_from_plain(self, plain: str) -> None:
        # usihifadhi plaintext kamwe; weka hint ndogo tu kwa uchunguzi
        self.token_hash = self.hash_token(plain)
        self.token_hint = (plain or "")[:8] or None

    def generate_token(self, *, length: int = 32) -> str:
        token = secrets.token_urlsafe(length)
        self.set_token_from_plain(token)
        return token

    def verify_token(self, plain: str) -> bool:
        expected = self.token_hash or ""
        candidate = self.hash_token(plain or "")
        # constant-time comparison
        return hmac.compare_digest(expected, candidate)

    # ---------- Policy helpers ----------
    def _ip_allowed(self, ip: Optional[str]) -> bool:
        if self.lock_ip and ip and ip != self.lock_ip:
            return False
        if self.ip_allowlist:
            allow = {s.strip() for s in self.ip_allowlist if isinstance(s, str)}
            if not ip or ip not in allow:
                return False
        return True

    def _ua_allowed(self, ua: Optional[str]) -> bool:
        if self.ua_substring and ua:
            return self.ua_substring.lower() in ua.lower()
        return True

    def validate_context(self, *, ip: Optional[str], user_agent: Optional[str]) -> bool:
        return self._ip_allowed(ip) and self._ua_allowed(user_agent)

    # ---------- Consume / revoke / extend ----------
    def consume(self, *, ip: Optional[str] = None, user_agent: Optional[str] = None) -> bool:
        # Refresh status kama muda umeisha
        if self.is_expired and self.status != MagicLinkStatus.expired:
            self.status = MagicLinkStatus.expired

        if not self.consumable:
            return False
        if not self.validate_context(ip=ip, user_agent=user_agent):
            return False

        self.used_count = (self.used_count or 0) + 1
        self.used_at = _utcnow()
        self.consumed_ip = (ip or "").strip() or None
        self.consumed_ua = (user_agent or "").strip() or None

        if self.remaining_uses == 0:
            self.used = True
            self.status = MagicLinkStatus.used
        return True

    def revoke(self) -> None:
        self.status = MagicLinkStatus.revoked
        self.revoked_at = _utcnow()
        self.used = True

    def extend_expiry(self, *, minutes: int) -> None:
        """Panua muda wa kuisha kuanzia sasa (kwa approval ya admin)."""
        now = _utcnow()
        base = self.expires_at if (self.expires_at and self.expires_at > now) else now
        self.expires_at = base + dt.timedelta(minutes=max(1, int(minutes)))

    def __repr__(self) -> str:  # pragma: no cover
        state = f"status={self.status} used={self.used_count}/{self.max_uses}"
        return f"<MagicLink id={self.id} user={self.user_id} hint={self.token_hint!r} {state} exp={self.expires_at}>"

    # -------- Indices & Constraints --------
    # (zimewekwa juu kwenye __table_args__)


# ---------- Validators / Normalizers ----------
@validates("ua_substring", "lock_ip", "request_id", "idempotency_key", "consumed_ip", "consumed_ua", "token_hint")
def _trim_short_texts(_inst, _key, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    return v or None

@validates("ip_allowlist")
def _normalize_allowlist(_inst, _key, value: Optional[Sequence[str]]):
    if not value:
        return None
    cleaned = []
    for x in value:
        if isinstance(x, str):
            s = x.strip()
            if s:
                cleaned.append(s)
    return cleaned or None


# ---------- Event hooks ----------
from sqlalchemy.event import listens_for  # keep local, epuka kurudia import juu

@listens_for(MagicLink, "before_insert")
def _ml_before_insert(_m, _c, t: MagicLink) -> None:
    # Weka expiry default (dakika 15) kama haijawekwa
    if not t.expires_at:
        t.expires_at = _utcnow() + dt.timedelta(minutes=int(os.getenv("MAGIC_LINK_TTL_MIN", "15")))
    # Normalize strings
    if t.ua_substring:
        t.ua_substring = t.ua_substring.strip() or None
    if t.lock_ip:
        t.lock_ip = t.lock_ip.strip() or None
    if t.request_id:
        t.request_id = t.request_id.strip() or None
    if t.idempotency_key:
        t.idempotency_key = t.idempotency_key.strip() or None
    if t.token_hint:
        t.token_hint = t.token_hint.strip() or None

@listens_for(MagicLink, "before_update")
def _ml_before_update(_m, _c, t: MagicLink) -> None:
    # Kama imepitwa na muda wakati wa update, weka status expired
    if t.is_expired and t.status not in (MagicLinkStatus.used, MagicLinkStatus.revoked, MagicLinkStatus.expired):
        t.status = MagicLinkStatus.expired
    # Normalization ndogo
    if t.ua_substring:
        t.ua_substring = t.ua_substring.strip() or None
    if t.lock_ip:
        t.lock_ip = t.lock_ip.strip() or None
