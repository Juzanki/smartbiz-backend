# backend/models/badge_history.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, TYPE_CHECKING, Dict, Any, List

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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, Session, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for
from sqlalchemy.ext.mutable import MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

if TYPE_CHECKING:
    from .user import User


# ───────── Enums ─────────
class BadgeSource(str, enum.Enum):
    system = "system"
    leaderboard = "leaderboard"
    purchase = "purchase"
    manual = "manual"


class BadgeEventType(str, enum.Enum):
    awarded = "awarded"
    renewed = "renewed"
    expired = "expired"
    revoked = "revoked"


# ───────── Model ─────────
class BadgeHistory(Base):
    """
    Tukio la beji (awarded/renewed/revoked/expired).
    NB: Hii ni historia; usizuie duplicates kimakusudi (analytics hutegemea).
    Tunadhibiti duplicates “hai” kwa partial-unique (Postgres) au kupitia service logic.
    """
    __tablename__ = "badge_history"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Mmiliki wa beji (mpokeaji)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Aliyetoa (hiari)
    awarded_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # Utambulisho wa beji
    badge_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)   # key ya program
    badge_name: Mapped[Optional[str]] = mapped_column(String(120))                    # label ya UI
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Chanzo/Sababu
    source: Mapped[BadgeSource] = mapped_column(
        SQLEnum(BadgeSource, name="badge_source", native_enum=False, validate_strings=True),
        default=BadgeSource.system,
        nullable=False,
        index=True,
    )
    reason: Mapped[Optional[str]] = mapped_column(String(160))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # Aina ya tukio (kwa urahisi wa analytics)
    event_type: Mapped[BadgeEventType] = mapped_column(
        SQLEnum(BadgeEventType, name="badge_event_type", native_enum=False, validate_strings=True),
        default=BadgeEventType.awarded,
        nullable=False,
        index=True,
    )

    # Metadata ya ziada (icon, image, proof, tx_id, n.k.)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))

    # Timestamps & lifecycle
    awarded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[Optional[str]] = mapped_column(String(160))

    # ───── Relationships (disambiguated) ─────
    user: Mapped["User"] = relationship(
        "User",
        back_populates="badge_events_received",
        foreign_keys="BadgeHistory.user_id",
        passive_deletes=True,
        lazy="selectin",
    )
    awarded_by: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="badge_events_given",
        foreign_keys="BadgeHistory.awarded_by_id",
        lazy="selectin",
    )

    # ───── Helpers / Hybrids ─────
    @hybrid_property
    def is_expired(self) -> bool:
        return bool(self.expires_at and dt.datetime.now(dt.timezone.utc) >= self.expires_at)

    @hybrid_property
    def is_active(self) -> bool:
        """‘Hai’ ikiwa haija-expire na haijarevokewa."""
        return (not self.revoked) and (not self.is_expired)

    @property
    def time_to_expiry(self) -> Optional[dt.timedelta]:
        if not self.expires_at:
            return None
        return self.expires_at - dt.datetime.now(dt.timezone.utc)

    # ———— Domain helpers ————
    def revoke(self, reason: str | None = None) -> None:
        self.revoked = True
        if self.revoked_at is None:
            self.revoked_at = dt.datetime.now(dt.timezone.utc)
        if reason:
            self.revoked_reason = reason
        self.event_type = BadgeEventType.revoked

    def renew(self, *, new_expiry: Optional[dt.datetime]) -> None:
        """Badilisha/ongeza muda wa kuisha; weka event type ya 'renewed'."""
        self.expires_at = new_expiry
        self.event_type = BadgeEventType.renewed

    def expire_now(self) -> None:
        """Tangaza muda wa kuisha sasa (haiti seti revoked)."""
        self.expires_at = dt.datetime.now(dt.timezone.utc)
        self.event_type = BadgeEventType.expired

    @validates("revoked")
    def _auto_timestamp_revoked(self, _key, value: bool):
        if value and self.revoked_at is None:
            self.revoked_at = dt.datetime.now(dt.timezone.utc)
        return value

    @validates("badge_code", "badge_name", "reason")
    def _trim_strings(self, key: str, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        v = value.strip()
        max_len = {"badge_code": 64, "badge_name": 120, "reason": 160}.get(key, None)
        return v[:max_len] if max_len else v

    # ───── Query helpers ─────
    @classmethod
    def award(
        cls,
        session: Session,
        *,
        user_id: int,
        badge_code: str,
        badge_name: Optional[str] = None,
        level: int = 1,
        source: BadgeSource = BadgeSource.system,
        reason: Optional[str] = None,
        notes: Optional[str] = None,
        awarded_by_id: Optional[int] = None,
        expires_at: Optional[dt.datetime] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> "BadgeHistory":
        ev = cls(
            user_id=user_id,
            badge_code=(badge_code or "").strip()[:64],
            badge_name=(badge_name or "").strip()[:120] or None,
            level=max(1, int(level or 1)),
            source=source,
            reason=(reason or "").strip()[:160] or None,
            notes=(notes or "").strip() or None,
            awarded_by_id=awarded_by_id,
            expires_at=expires_at,
            meta=(meta or None),
            event_type=BadgeEventType.awarded,
        )
        session.add(ev)
        return ev

    @classmethod
    def award_once_active(
        cls,
        session: Session,
        *,
        user_id: int,
        badge_code: str,
        **kwargs,
    ) -> "BadgeHistory":
        """
        Toa beji ikiwa hakuna “active” duplicate ya badge_code kwa user huyu.
        (Kwenye Postgres, partial unique itasaidia; SQLite: tumia helper hii.)
        """
        now = func.now()
        exists_active = (
            session.query(cls.id)
            .filter(
                cls.user_id == user_id,
                cls.badge_code == badge_code,
                cls.revoked.is_(False),
                func.coalesce(cls.expires_at, now) > now,
            )
            .limit(1)
            .first()
            is not None
        )
        if exists_active:
            # bado ongeza rekodi ya “renewed”/“awarded” kulingana na mahitaji?
            # hapa chaguo: rudisha rekodi ya mwisho.
            row = (
                session.query(cls)
                .filter(cls.user_id == user_id, cls.badge_code == badge_code)
                .order_by(cls.awarded_at.desc())
                .first()
            )
            return row  # type: ignore[return-value]
        return cls.award(session, user_id=user_id, badge_code=badge_code, **kwargs)

    @classmethod
    def active_for_user(cls, session: Session, user_id: int) -> List["BadgeHistory"]:
        now = func.now()
        return (
            session.query(cls)
            .filter(
                cls.user_id == user_id,
                cls.revoked.is_(False),
                func.coalesce(cls.expires_at, now) > now,
            )
            .order_by(cls.awarded_at.desc())
            .all()
        )

    @classmethod
    def latest_for(cls, session: Session, user_id: int, badge_code: str) -> Optional["BadgeHistory"]:
        return (
            session.query(cls)
            .filter(cls.user_id == user_id, cls.badge_code == badge_code)
            .order_by(cls.awarded_at.desc())
            .limit(1)
            .one_or_none()
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "awarded_by_id": self.awarded_by_id,
            "badge_code": self.badge_code,
            "badge_name": self.badge_name,
            "level": self.level,
            "source": self.source.value,
            "reason": self.reason,
            "notes": self.notes,
            "event_type": self.event_type.value,
            "meta": self.meta or {},
            "awarded_at": self.awarded_at.isoformat() if self.awarded_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked": self.revoked,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revoked_reason": self.revoked_reason,
            "is_active": self.is_active,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<BadgeHistory id={self.id} user={self.user_id} "
            f"code={self.badge_code} level={self.level} active={self.is_active}>"
        )

    # ───── Table-level constraints & indexes ─────
    __table_args__ = (
        CheckConstraint("level >= 1", name="ck_badgehist_level_min1"),
        CheckConstraint("length(trim(badge_code)) >= 2", name="ck_badgehist_code_len"),
        CheckConstraint(
            "(expires_at IS NULL) OR (awarded_at IS NULL) OR (expires_at > awarded_at)",
            name="ck_badgehist_expires_after_award",
        ),
        CheckConstraint("NOT revoked OR revoked_at IS NOT NULL", name="ck_badgehist_revoked_has_ts"),
        Index("ix_badgehist_user_time", "user_id", "awarded_at"),
        Index("ix_badgehist_awarder_time", "awarded_by_id", "awarded_at"),
        Index("ix_badgehist_badge_code_level", "badge_code", "level"),
        Index("ix_badgehist_user_badge", "user_id", "badge_code"),
        # (Postgres tu) partial unique: active duplicate zisiruhusiwe
        Index(
            "uq_badgehist_active_once",
            "user_id",
            "badge_code",
            unique=True,
            postgresql_where=text("NOT revoked AND (expires_at IS NULL OR expires_at > now())"),
        ),
    )


# ───── Normalizers / Guards ─────
@listens_for(BadgeHistory, "before_insert")
def _bh_before_insert(_m, _c, t: BadgeHistory) -> None:
    # safisha strings za msingi
    if t.badge_code:
        t.badge_code = t.badge_code.strip()[:64]
    if t.badge_name:
        t.badge_name = (t.badge_name or "").strip()[:120] or None
    if t.reason:
        t.reason = (t.reason or "").strip()[:160] or None
    if t.notes:
        t.notes = (t.notes or "").strip() or None
    if t.revoked and not t.revoked_at:
        t.revoked_at = dt.datetime.now(dt.timezone.utc)


@listens_for(BadgeHistory, "before_update")
def _bh_before_update(_m, _c, t: BadgeHistory) -> None:
    if t.badge_code:
        t.badge_code = t.badge_code.strip()[:64]
    if t.badge_name:
        t.badge_name = (t.badge_name or "").strip()[:120] or None
    if t.reason:
        t.reason = (t.reason or "").strip()[:160] or None
    if t.notes:
        t.notes = (t.notes or "").strip() or None
    if t.revoked and not t.revoked_at:
        t.revoked_at = dt.datetime.now(dt.timezone.utc)
