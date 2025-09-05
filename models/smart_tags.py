# backend/models/tag.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional, Iterable, Dict, Any, List, Tuple, TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    event,
    select,
    insert,
    delete,
    Table,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, Session
from sqlalchemy import inspect as sa_inspect

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

if TYPE_CHECKING:
    from .customer import Customer  # optional; ensures type hints only

# ---------- JSON type (PG -> JSONB/JSON_VARIANT; others -> generic JSON) ----------
try:
    pass
except Exception:  # pragma: no cover
    # patched: use shared JSON_VARIANT
    pass

# ---------- Helpers ----------
HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
SLUG_RE = re.compile(r"[^a-z0-9\-]+")

def _slugify(value: str) -> str:
    v = (value or "").strip().lower()
    v = v.replace(" ", "-")
    v = SLUG_RE.sub("", v)
    v = v.strip("-")
    return v[:50] or "tag"

def _normalize_hex(hex_code: str) -> str:
    """Accept #RGB or #RRGGBB -> normalize to #RRGGBB."""
    v = (hex_code or "").strip()
    if not HEX_COLOR_RE.match(v):
        raise ValueError("Color must be a valid hex (#RGB or #RRGGBB)")
    if len(v) == 4:  # #RGB -> #RRGGBB
        r, g, b = v[1], v[2], v[3]
        return f"#{r}{r}{g}{g}{b}{b}".upper()
    return v.upper()

# ---------- Canonical association table reference ----------
# Meza "customer_tags" inapaswa KUWEPO (mf. kwenye smart_tags.py).
CUSTOMER_TAGS_TABLE: Optional[Table] = Base.metadata.tables.get("customer_tags")
if CUSTOMER_TAGS_TABLE is None:
    try:
        from .smart_tags import CustomerTag as _CT  # noqa: F401
        CUSTOMER_TAGS_TABLE = Base.metadata.tables.get("customer_tags")
    except Exception:  # pragma: no cover
        # itaibuliwa baadaye kwenye _ct_table() ikiwa haipo
        CUSTOMER_TAGS_TABLE = None

# ------------------------------ Tag ------------------------------
class Tag(Base):
    """
    User-scoped labels to segment customers.

    - Uniqueness: (user_id, name) & (user_id, slug)
    - Color validation (#RGB/#RRGGBB) + normalization to #RRGGBB
    - Usage metrics (usage_count, last_used_at)
    - JSON metadata (portable JSON/JSON_VARIANT, mutable)
    - Robust slug handling (deterministic + collision-avoidance per user)
    """
    __tablename__ = "tags"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_tag_user_name"),
        UniqueConstraint("user_id", "slug", name="uq_tag_user_slug"),
        Index("ix_tags_user_name", "user_id", "name"),
        Index("ix_tags_user_slug", "user_id", "slug"),
        Index("ix_tags_user_archived", "user_id", "archived"),
        Index("ix_tags_created", "created_at"),
        CheckConstraint("length(name) > 0", name="ck_tag_name_nonempty"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Identity
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    slug: Mapped[str] = mapped_column(String(50), nullable=False, index=True, default="tag")

    # Presentation
    color: Mapped[str] = mapped_column(String(10), default="#00A8E8", nullable=False)

    # Metrics / lifecycle
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    archived: Mapped[bool] = mapped_column(default=False, nullable=False)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)

    # Extensible metadata (mutable JSON to track in-place changes)
    meta: Mapped[Dict[str, Any]] = mapped_column(as_mutable_json(JSON_VARIANT), default=dict)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships (tumia meza ya "customer_tags" iliyopo)
    customers: Mapped[List["Customer"]] = relationship(
        "Customer",
        secondary="customer_tags",
        back_populates="tags",
        lazy="selectin",
    )

    # -------------------------- Helpers --------------------------
    @staticmethod
    def _validate_color(value: str) -> str:
        return _normalize_hex(value or "#00A8E8")

    def rename(self, new_name: str) -> None:
        n = (new_name or "").strip()
        if not n:
            raise ValueError("Name cannot be empty")
        self.name = n  # slug husasishwa na listener ya name:set

    def recolor(self, hex_code: str) -> None:
        self.color = self._validate_color(hex_code)

    def archive(self) -> None:
        self.archived = True
        self.archived_at = datetime.now(timezone.utc)

    def restore(self) -> None:
        self.archived = False
        self.archived_at = None

    def touch_usage(self) -> None:
        self.usage_count += 1
        self.last_used_at = datetime.now(timezone.utc)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "color": self.color,
            "usage_count": self.usage_count,
            "archived": self.archived,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    # ---- Bulk assignment helpers on association table ----
    def _ct_table(self) -> Table:
        if CUSTOMER_TAGS_TABLE is None:
            raise RuntimeError(
                "Association table 'customer_tags' haijapatikana kwenye Base.metadata. "
                "Hakikisha imeundwa (mf. smart_tags.py) kabla ya kutumia Tag helpers."
            )
        return CUSTOMER_TAGS_TABLE

    def add_customers(
        self,
        db: Session,
        customer_ids: Iterable[int],
        *,
        assigned_by: Optional[int] = None,
        source: Optional[str] = None,
        note: Optional[str] = None,
    ) -> int:
        ids = list({int(c) for c in (customer_ids or []) if c is not None})
        if not ids:
            return 0

        ct = self._ct_table()

        existing_rows = db.execute(
            select(ct.c.customer_id).where(ct.c.tag_id == self.id, ct.c.customer_id.in_(ids))
        ).scalars().all()
        existing = set(existing_rows)

        now = datetime.now(timezone.utc)
        rows: List[Dict[str, Any]] = []
        for c_id in ids:
            if c_id in existing:
                continue
            payload = {
                "customer_id": c_id,
                "tag_id": self.id,
                "assigned_by": assigned_by,
                "assigned_at": now,
                "source": (source or None),
                "note": (note or None),
            }
            # drop optional cols absent in CT table
            for col in ("assigned_by", "assigned_at", "source", "note"):
                if col not in ct.c:
                    payload.pop(col, None)
            rows.append(payload)

        if rows:
            db.execute(insert(ct), rows)
            self.usage_count += len(rows)
            self.last_used_at = now
        return len(rows)

    def remove_customers(self, db: Session, customer_ids: Iterable[int]) -> int:
        ids = list({int(c) for c in (customer_ids or []) if c is not None})
        if not ids:
            return 0
        ct = self._ct_table()
        res = db.execute(
            delete(ct).where(ct.c.tag_id == self.id, ct.c.customer_id.in_(ids))
        )
        deleted = int(res.rowcount or 0)
        if deleted:
            self.usage_count = max(0, self.usage_count - deleted)
        return deleted

    # ---- Convenience classmethods ----
    @classmethod
    def get_or_create(cls, db: Session, *, user_id: int, name: str, color: Optional[str] = None) -> "Tag":
        """Tafuta tag ya jina hili kwa user; kama haipo iunde kwa usalama."""
        tag = db.scalar(select(cls).where(cls.user_id == user_id, cls.name == name.strip()))
        if tag:
            return tag
        tag = cls(user_id=user_id, name=name.strip(), color=_normalize_hex(color or "#00A8E8"))
        db.add(tag)
        db.flush()  # ili ipate id na slug unique kabla ya matumizi zaidi
        return tag

    @classmethod
    def search(cls, db: Session, *, user_id: int, q: str, limit: int = 20) -> List["Tag"]:
        """Utafutaji mwepesi kwa jina/slug (ILIKE/LIKE portable)."""
        pat = f"%{(q or '').strip().lower()}%"
        return list(
            db.scalars(
                select(cls)
                .where(cls.user_id == user_id)
                .where(func.lower(cls.name).like(pat) | func.lower(cls.slug).like(pat))
                .order_by(cls.archived.asc(), cls.name.asc())
                .limit(max(1, min(100, limit)))
            )
        )

    def merge_into(self, db: Session, *, target: "Tag") -> int:
        """
        Unganisha tag hii ndani ya target (reassign associations), kisha i-archive hii.
        Inarudisha idadi ya rows zilizohamishwa kwenye association table.
        """
        if self.id == target.id or self.user_id != target.user_id:
            return 0
        ct = self._ct_table()
        # move unique pairs
        moved = db.execute(
            insert(ct)
            .from_select(
                [ct.c.customer_id, ct.c.tag_id],
                select(ct.c.customer_id, func.cast(target.id, Integer)).where(ct.c.tag_id == self.id),
            )
            .prefix_with("OR IGNORE")  # SQLite
        )
        # delete old links
        db.execute(delete(ct).where(ct.c.tag_id == self.id))
        self.archive()
        return int(moved.rowcount or 0)

    # ---- Slug utilities ----
    def ensure_slug(self) -> None:
        """Hakikisha slug ipo na imesafishwa (but don’t change if already custom & unique)."""
        if not self.slug or self.slug == "tag":
            self.slug = _slugify(self.name or self.slug)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tag id={self.id} user={self.user_id} name={self.name} color={self.color}>"

# ----------------------- Attribute & row listeners -----------------------
@event.listens_for(Tag.name, "set", retval=True)
def _normalize_name(target: Tag, value: str, oldvalue, initiator):
    v = (value or "").strip()
    if not v:
        raise ValueError("Tag name cannot be empty")
    normalized = v[:50]
    # update slug only if slug was derived from old name or blank/default
    old_slug_derived = (oldvalue is None) or (_slugify(oldvalue or "") == (target.slug or "")) or (target.slug in (None, "", "tag"))
    if old_slug_derived:
        target.slug = _slugify(normalized)
    return normalized

@event.listens_for(Tag.color, "set", retval=True)
def _normalize_color(target: Tag, value: str, oldvalue, initiator):
    return Tag._validate_color(value or "#00A8E8")

def _resolve_unique_slug(conn, user_id: int, base: str) -> str:
    # Pata suffix kwa kuepuka mgongano wa (user_id, slug)
    base = _slugify(base)
    # count slugs starting with base or base-<num>
    stmt = select(func.count()).select_from(Tag).where(Tag.user_id == user_id, Tag.slug.like(f"{base}%"))
    count = conn.execute(stmt).scalar() or 0
    return base if count == 0 else f"{base}-{count+1}"

@event.listens_for(Tag, "before_insert")
def _tag_before_insert(mapper, connection, target: Tag) -> None:
    target.ensure_slug()
    if target.meta is None:
        target.meta = {}
    # guarantee uniqueness per user
    target.slug = _resolve_unique_slug(connection, target.user_id, target.slug or target.name)

@event.listens_for(Tag, "before_update")
def _tag_before_update(mapper, connection, target: Tag) -> None:
    if target.meta is None:
        target.meta = {}
    insp = sa_inspect(target)
    # Only recompute unique slug if slug field actually changed (avoid churn)
    if "slug" in insp.attrs and insp.attrs.slug.history.has_changes():
        target.slug = _slugify(target.slug or target.name)
        target.slug = _resolve_unique_slug(connection, target.user_id, target.slug)
