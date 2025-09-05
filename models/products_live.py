# backend/models/products_live.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from decimal import Decimal
from typing import Optional, TYPE_CHECKING, Dict, Any, List, Iterable

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
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates, Session
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

if TYPE_CHECKING:
    from .live_stream import LiveStream
    from .product import Product


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class ShowcaseState(str, enum.Enum):
    queued = "queued"     # ipo foleni, haijaonekana
    live   = "live"       # inaonekana sasa
    hidden = "hidden"     # imesitishwa bila kuhitimishwa (unaweza kui-revive)
    ended  = "ended"      # imekamilika (kwa ripoti)


class ShowcaseMode(str, enum.Enum):
    single = "single"     # kipengee kimoja (classic pin)
    grid   = "grid"       # vitu vingi kwa wakati mmoja
    carousel = "carousel" # rotation/auto-scroll client-side


class LiveProduct(Base):
    """
    Uhusiano wa LiveStream <-> Product ukibeba metadata ya 'live showcase'.

    Vipengele vikuu:
      - **Slots**: uwezo wa kuonyesha vitu vingi mara moja (slot 0..N) + 'mode'
      - **Snapshots**: jina/sku/bei/picha/stok katika wakati wa onyesho
      - **Overrides**: bei ya kampeni (discount_pct/amount), CTA, availability
      - **Metrics**: clicks / carts / units / gross + CTR & AOV (hybrids)
      - **Ops helpers**: pin/unpin, start/stop, re-order, bulk rotation
      - **Scheduling**: deliver_after/expires_at (dirisha la kuonekana)
    """
    __tablename__ = "live_products"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        # Uniqueness & ordering
        UniqueConstraint("stream_id", "position", name="uq_liveprod_stream_position"),
        # Slot semantics: si unique (unaweza kuwa na queued nyingi kwenye slot moja), lakini hot path index
        Index("ix_liveprod_stream_state", "stream_id", "state"),
        Index("ix_liveprod_stream_slot_state", "stream_id", "slot", "state"),
        Index("ix_liveprod_product_created", "product_id", "created_at"),
        Index("ix_liveprod_stream_created", "stream_id", "created_at"),
        Index("ix_liveprod_visibility_time", "is_visible", "started_at"),
        # Guards
        CheckConstraint("length(trim(currency)) = 3", name="ck_liveprod_currency_iso3"),
        CheckConstraint("position >= 0", name="ck_liveprod_position_nonneg"),
        CheckConstraint("slot >= 0", name="ck_liveprod_slot_nonneg"),
        CheckConstraint(
            "clicks_count >= 0 AND add_to_cart >= 0 AND purchases_count >= 0 AND units_sold >= 0",
            name="ck_liveprod_metrics_nonneg",
        ),
        CheckConstraint(
            "(ended_at IS NULL) OR (started_at IS NULL) OR (ended_at >= started_at)",
            name="ck_liveprod_end_after_start",
        ),
        CheckConstraint(
            "(expires_at IS NULL) OR (deliver_after IS NULL) OR (expires_at > deliver_after)",
            name="ck_liveprod_expiry_after_deliver_after",
        ),
        CheckConstraint(
            "(campaign_discount_pct IS NULL) OR (campaign_discount_pct BETWEEN 0 AND 100)",
            name="ck_liveprod_discount_pct_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Chanzo: stream
    stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream",
        back_populates="live_products",
        passive_deletes=True,
        lazy="selectin",
    )

    # Bidhaa (snapshot hubaki hata bidhaa ikifutwa)
    product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    product: Mapped[Optional["Product"]] = relationship(
        "Product",
        lazy="selectin",
        passive_deletes=True,
    )

    # Uonyeshaji: slot & mode
    mode: Mapped[ShowcaseMode] = mapped_column(
        SQLEnum(ShowcaseMode, name="live_showcase_mode", native_enum=False, validate_strings=True),
        default=ShowcaseMode.grid,
        nullable=False,
        index=True,
    )
    slot: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), index=True)  # 0..N
    state: Mapped[ShowcaseState] = mapped_column(
        SQLEnum(ShowcaseState, name="live_showcase_state", native_enum=False, validate_strings=True),
        default=ShowcaseState.queued,
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), index=True)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    pinned_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    unpinned_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Snapshots & Overrides
    product_name: Mapped[Optional[str]] = mapped_column(String(255))
    product_sku:  Mapped[Optional[str]] = mapped_column(String(64), index=True)
    currency:     Mapped[str] = mapped_column(String(3), default="TZS", nullable=False, index=True)
    price_snapshot: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))  # bei ya wakati huo
    stock_snapshot: Mapped[Optional[int]] = mapped_column(Integer)  # stok wakati wa snapshot
    price_override: Mapped[Optional[Decimal]] = mapped_column(DECIMAL_TYPE)   # override absolute
    campaign_discount_pct: Mapped[Optional[int]] = mapped_column(Integer)     # 0..100
    campaign_label: Mapped[Optional[str]] = mapped_column(String(64))

    # Media & CTA
    image_url:  Mapped[Optional[str]] = mapped_column(String(512))
    caption:    Mapped[Optional[str]] = mapped_column(Text)
    cta_label:  Mapped[Optional[str]] = mapped_column(String(48))
    cta_url:    Mapped[Optional[str]] = mapped_column(String(512))
    meta:       Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Metrics (analytics)
    impressions:     Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    clicks_count:    Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    add_to_cart:     Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    purchases_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    units_sold:      Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    gross_sales:     Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # Dirisha & maisha ya onyesho
    deliver_after: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    expires_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    showcased_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    ended_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def effective_price(self) -> Decimal:
        """Bei ya kuonyesha kwa watazamaji (override > discount% > snapshot)."""
        base = self.price_override if self.price_override is not None else self.price_snapshot or Decimal("0")
        if self.price_override is None and (self.campaign_discount_pct or 0) > 0:
            try:
                pct = Decimal(str(self.campaign_discount_pct)) / Decimal("100")
            except Exception:
                pct = Decimal("0")
            base = (self.price_snapshot or Decimal("0")) * (Decimal("1") - pct)
        return (base or Decimal("0"))

    @hybrid_property
    def ctr(self) -> float:
        imp = max(1, int(self.impressions or 0))
        return float((self.clicks_count or 0) * 100.0 / imp)

    @hybrid_property
    def aov(self) -> Decimal:
        """Average order value for this item context = gross / purchases (guarded)."""
        p = max(1, int(self.purchases_count or 0))
        return (self.gross_sales or Decimal("0")) / Decimal(p)

    @hybrid_property
    def is_live_now(self) -> bool:
        now = _utcnow()
        if self.state != ShowcaseState.live:
            return False
        if self.deliver_after and now < self.deliver_after:
            return False
        if self.expires_at and now >= self.expires_at:
            return False
        return True

    # ---------- Validations ----------
    @validates("currency")
    def _v_currency(self, _k: str, v: str) -> str:
        v2 = (v or "").upper().strip()
        if len(v2) != 3:
            raise ValueError("currency must be a 3-letter ISO code (e.g., TZS, USD)")
        return v2

    @validates("slot", "position")
    def _v_nonneg(self, _k: str, v: int) -> int:
        return max(0, int(v or 0))

    # ---------- Metrics updaters ----------
    def track_impression(self, n: int = 1) -> None:
        self.impressions = (self.impressions or 0) + max(0, int(n))

    def track_click(self, n: int = 1) -> None:
        self.clicks_count = (self.clicks_count or 0) + max(0, int(n))

    def track_add_to_cart(self, n: int = 1) -> None:
        self.add_to_cart = (self.add_to_cart or 0) + max(0, int(n))

    def track_purchase(self, *, units: int = 1, gross: Decimal | int | float | str | None = None) -> None:
        self.purchases_count = (self.purchases_count or 0) + 1
        self.units_sold = (self.units_sold or 0) + max(0, int(units))
        if gross is not None:
            g = gross if isinstance(gross, Decimal) else Decimal(str(gross))
            self.gross_sales = (self.gross_sales or Decimal("0")) + max(Decimal("0"), g)

    # ---------- Lifecycle / visibility ----------
    def start(self, *, slot: Optional[int] = None) -> None:
        if slot is not None:
            self.slot = max(0, int(slot))
        self.state = ShowcaseState.live
        self.is_visible = True
        self.started_at = self.started_at or _utcnow()

    def stop(self) -> None:
        self.state = ShowcaseState.ended
        self.ended_at = self.ended_at or _utcnow()
        self.is_pinned = False
        self.is_visible = False
        self.unpinned_at = self.unpinned_at or _utcnow()

    def hide(self) -> None:
        self.state = ShowcaseState.hidden
        self.is_visible = False

    def queue(self, *, position: Optional[int] = None) -> None:
        self.state = ShowcaseState.queued
        self.is_visible = False
        if position is not None:
            self.position = max(0, int(position))

    def pin(self) -> None:
        self.is_pinned = True
        self.pinned_at = self.pinned_at or _utcnow()

    def unpin(self) -> None:
        self.is_pinned = False
        self.unpinned_at = _utcnow()

    def set_window(self, *, deliver_after: Optional[dt.datetime] = None, expires_at: Optional[dt.datetime] = None) -> None:
        self.deliver_after = deliver_after
        self.expires_at = expires_at

    # ---------- Bulk/rotation helpers (static-ish ops) ----------
    @staticmethod
    def reorder(session: Session, *, stream_id: int, ids_in_order: Iterable[int]) -> None:
        """
        Pangilia `position` kwa id zilizotolewa (0..N). Hazipo kwenye list haziguswi.
        """
        for idx, rid in enumerate(ids_in_order):
            session.query(LiveProduct).filter_by(id=int(rid), stream_id=stream_id).update({"position": idx})
        session.flush()

    @staticmethod
    def clear_slot(session: Session, *, stream_id: int, slot: int) -> int:
        """
        Ficha 'live' zote kwenye slot fulani bila kuzimaliza (state->hidden, visible=False).
        Inarudisha idadi ya zilizoguswa.
        """
        q = session.query(LiveProduct).filter(
            LiveProduct.stream_id == stream_id,
            LiveProduct.slot == int(slot),
            LiveProduct.state == ShowcaseState.live,
        )
        count = 0
        for lp in q:
            lp.hide()
            count += 1
        session.flush()
        return count

    @staticmethod
    def go_live_multi(
        session: Session,
        *,
        stream_id: int,
        items: List[int],
        slot: int = 0,
        mode: ShowcaseMode = ShowcaseMode.grid,
        clear_slot_first: bool = False,
    ) -> List[int]:
        """
        Weka vitu kadhaa live kwa slot mmoja (grid/carousel): hutumika kwa “multi-product spotlight”.
        - Ikiwa `clear_slot_first` ni True, huficha vya zamani kwanza.
        - Huweka `is_visible=True` na `state=live` kwa kila.
        - Hurudisha orodha ya IDs zilizowashwa.
        """
        changed: List[int] = []
        if clear_slot_first:
            LiveProduct.clear_slot(session, stream_id=stream_id, slot=slot)

        q = session.query(LiveProduct).filter(
            LiveProduct.stream_id == stream_id,
            LiveProduct.id.in_(list(map(int, items))),
        )
        now = _utcnow()
        for lp in q:
            lp.mode = mode
            lp.slot = max(0, int(slot))
            lp.state = ShowcaseState.live
            lp.is_visible = True
            lp.started_at = lp.started_at or now
            changed.append(lp.id)
        session.flush()
        return changed

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<LiveProduct id={self.id} stream={self.stream_id} "
            f"product={self.product_id} state={self.state} slot={self.slot} pos={self.position}>"
        )
