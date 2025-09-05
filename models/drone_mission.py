# backend/models/drone_mission.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import math
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING, Dict, Any, Iterable

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
    text,
    Numeric as SA_NUMERIC,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

if TYPE_CHECKING:
    from .user import User
    from .product import Product  # Product.missions back_populates="product"

# ---------- NUMERIC helpers (portable PG/SQLite) ----------
def _num(prec: int, scale: int):
    try:
        from sqlalchemy.dialects.postgresql import NUMERIC as PG_NUMERIC  # type: ignore
        return SA_NUMERIC(prec, scale).with_variant(PG_NUMERIC(prec, scale), "postgresql")
    except Exception:  # pragma: no cover
        return SA_NUMERIC(prec, scale)

MONEY_TYPE  = _num(18, 2)
COORD_TYPE  = _num(10, 6)   # ±DDD.dddddd
ALT_TYPE    = _num(8, 2)    # meters (2dp)

# ---------- Enums ----------
class MissionStatus(str, enum.Enum):
    pending    = "pending"
    queued     = "queued"
    in_transit = "in_transit"
    delivered  = "delivered"
    failed     = "failed"
    canceled   = "canceled"

class MissionMode(str, enum.Enum):
    auto     = "auto"
    manual   = "manual"
    assisted = "assisted"  # partial autonomy

class MissionSource(str, enum.Enum):
    system = "system"
    user   = "user"
    api    = "api"


class DroneMission(Base):
    """
    Usimamizi wa safari ya drone: njia (waypoints), telemetry, na hali ya mchakato.

    • JSON_VARIANT/JSON (mutable): waypoints, telemetry_meta
    • NUMERIC portable kwa: kordinati (lat/lng), altitude (m)
    • Uthibitisho wa masafa (lat/lng/battery/rssi), na helpers za ETA/position/telemetry
    """
    __tablename__ = "drone_missions"
    __mapper_args__ = {"eager_defaults": True}

    # ----- IDs & links -----
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # Utambulisho wa drone (fleet/serial)
    drone_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)

    # Lengo & kordinati
    destination: Mapped[str] = mapped_column(String(240), nullable=False)
    dest_lat: Mapped[Optional[Decimal]] = mapped_column(COORD_TYPE)
    dest_lng: Mapped[Optional[Decimal]] = mapped_column(COORD_TYPE)

    # Hali, Mode, Chanzo
    status: Mapped[MissionStatus] = mapped_column(
        SQLEnum(MissionStatus, name="mission_status", native_enum=False, validate_strings=True),
        default=MissionStatus.pending,
        nullable=False,
        index=True,
    )
    mode: Mapped[MissionMode] = mapped_column(
        SQLEnum(MissionMode, name="mission_mode", native_enum=False, validate_strings=True),
        default=MissionMode.auto,
        nullable=False,
        index=True,
    )
    source: Mapped[MissionSource] = mapped_column(
        SQLEnum(MissionSource, name="mission_source", native_enum=False, validate_strings=True),
        default=MissionSource.system,
        nullable=False,
        index=True,
    )

    # Njia / Telemetry
    # Mfano wa waypoints: [{"lat":-6.8,"lng":39.2,"alt":60}, ...]
    waypoints: Mapped[Optional[list[dict]]] = mapped_column(
        MutableList.as_mutable(JSON_VARIANT)
    )
    current_lat: Mapped[Optional[Decimal]] = mapped_column(COORD_TYPE)
    current_lng: Mapped[Optional[Decimal]] = mapped_column(COORD_TYPE)
    current_alt_m: Mapped[Optional[Decimal]] = mapped_column(ALT_TYPE)  # meters
    battery_pct: Mapped[Optional[int]] = mapped_column(Integer)  # 0..100
    signal_rssi: Mapped[Optional[int]] = mapped_column(Integer)  # ~ -140..0 dBm
    telemetry_meta: Mapped[Optional[dict]] = mapped_column(
        MutableDict.as_mutable(JSON_VARIANT)  # speeds, headings, wind, n.k.
    )

    # Mipango / Udhibiti
    eta: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    emergency: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    retry_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Mihuri ya muda
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    queued_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    # Chanzo halisi (auditing)
    initiated_by: Mapped[MissionSource] = mapped_column(
        SQLEnum(MissionSource, name="mission_initiated_by", native_enum=False, validate_strings=True),
        default=MissionSource.system,
        nullable=False,
        index=True,
    )

    failure_reason: Mapped[Optional[str]] = mapped_column(String(200))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # ----- Relationships -----
    user: Mapped["User"] = relationship(
        "User",
        back_populates="drone_missions",
        foreign_keys=[user_id],
        passive_deletes=True,
        lazy="selectin",
    )
    product: Mapped["Product"] = relationship(
        "Product",
        back_populates="missions",
        foreign_keys=[product_id],
        lazy="selectin",
    )

    # ----- Hybrids -----
    @hybrid_property
    def is_terminal(self) -> bool:
        return self.status in {MissionStatus.delivered, MissionStatus.failed, MissionStatus.canceled}

    @hybrid_property
    def in_progress(self) -> bool:
        return self.status in {MissionStatus.queued, MissionStatus.in_transit}

    @hybrid_method
    def is_owner(self, uid: int) -> bool:
        return self.user_id == uid

    # ----- Helpers -----
    def enqueue(self) -> None:
        self.status = MissionStatus.queued
        self.queued_at = dt.datetime.now(dt.timezone.utc)

    def start(self) -> None:
        self.status = MissionStatus.in_transit
        now = dt.datetime.now(dt.timezone.utc)
        self.started_at = self.started_at or now
        if not self.queued_at:
            self.queued_at = now

    def mark_delivered(self) -> None:
        self.status = MissionStatus.delivered
        self.delivered_at = dt.datetime.now(dt.timezone.utc)
        self.emergency = False

    def mark_failed(self, reason: str | None = None) -> None:
        self.status = MissionStatus.failed
        self.failed_at = dt.datetime.now(dt.timezone.utc)
        if reason:
            self.failure_reason = reason[:200]

    def cancel(self, reason: str | None = None) -> None:
        self.status = MissionStatus.canceled
        self.canceled_at = dt.datetime.now(dt.timezone.utc)
        if reason:
            self.failure_reason = reason[:200]

    def set_eta(self, when: dt.datetime | None) -> None:
        self.eta = when

    def set_position(
        self,
        lat: Decimal | float | None,
        lng: Decimal | float | None,
        *,
        alt_m: Decimal | float | None = None,
    ) -> None:
        self.current_lat   = None if lat   is None else Decimal(str(lat))
        self.current_lng   = None if lng   is None else Decimal(str(lng))
        self.current_alt_m = None if alt_m is None else Decimal(str(alt_m))

    def bump_retry(self) -> int:
        self.retry_attempts = (self.retry_attempts or 0) + 1
        return self.retry_attempts

    def flag_emergency(self, on: bool = True) -> None:
        self.emergency = bool(on)

    def add_waypoints(self, wps: Iterable[Dict[str, Any]]) -> None:
        data = list(self.waypoints or [])
        for w in (wps or []):
            wp = {
                "lat": float(w.get("lat")) if w.get("lat") is not None else None,
                "lng": float(w.get("lng")) if w.get("lng") is not None else None,
                "alt": float(w.get("alt")) if w.get("alt") is not None else None,
            }
            if wp["lat"] is not None and wp["lng"] is not None:
                data.append(wp)
        self.waypoints = data

    def clear_waypoints(self) -> None:
        self.waypoints = []

    def merge_telemetry(self, updates: Dict[str, Any]) -> None:
        data = dict(self.telemetry_meta or {})
        for k, v in (updates or {}).items():
            data[str(k)] = v
        self.telemetry_meta = data

    # ---------- Distance & ETA (utility only; not persisted) ----------
    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371.0088  # mean Earth radius in km
        p1 = math.radians(lat1)
        p2 = math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    def distance_remaining_km(self) -> Optional[float]:
        if self.current_lat is None or self.current_lng is None or self.dest_lat is None or self.dest_lng is None:
            return None
        return float(
            self._haversine_km(float(self.current_lat), float(self.current_lng),
                               float(self.dest_lat), float(self.dest_lng))
        )

    def estimate_eta_by_speed_kmh(self, speed_kmh: float) -> Optional[dt.datetime]:
        """Kadiria ETA ukitumia kasi ya sasa (km/h)."""
        if speed_kmh <= 0:
            return None
        dist = self.distance_remaining_km()
        if dist is None:
            return None
        secs = int((dist / float(speed_kmh)) * 3600)
        return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=secs)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<DroneMission id={self.id} user={self.user_id} product={self.product_id} "
            f"status={self.status} dest={self.destination!r}>"
        )

    # ----- Validations -----
    @validates("battery_pct")
    def _validate_battery(self, _k: str, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        iv = int(v)
        if not (0 <= iv <= 100):
            raise ValueError("battery_pct must be between 0 and 100")
        return iv

    @validates("signal_rssi")
    def _validate_rssi(self, _k: str, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        iv = int(v)
        if not (-200 <= iv <= 50):
            raise ValueError("signal_rssi looks invalid (expected around -140..0)")
        return iv

    # ----- Constraints & Indexes -----
    __table_args__ = (
        Index("ix_mission_user_created", "user_id", "created_at"),
        Index("ix_mission_status_eta", "status", "eta"),
        Index("ix_mission_product_status", "product_id", "status"),
        Index("ix_mission_active_lookup", "user_id", "status", "product_id"),
        CheckConstraint("retry_attempts >= 0", name="ck_mission_retry_nonneg"),
        CheckConstraint(
            "battery_pct IS NULL OR (battery_pct BETWEEN 0 AND 100)",
            name="ck_mission_battery_range",
        ),
        CheckConstraint(
            "(dest_lat IS NULL AND dest_lng IS NULL) "
            "OR (dest_lat IS NOT NULL AND dest_lng IS NOT NULL)",
            name="ck_mission_dest_pair",
        ),
        CheckConstraint(
            "current_lat  IS NULL OR (current_lat  BETWEEN -90  AND 90)",
            name="ck_mission_cur_lat_range",
        ),
        CheckConstraint(
            "current_lng  IS NULL OR (current_lng  BETWEEN -180 AND 180)",
            name="ck_mission_cur_lng_range",
        ),
        CheckConstraint(
            "dest_lat     IS NULL OR (dest_lat     BETWEEN -90  AND 90)",
            name="ck_mission_dest_lat_range",
        ),
        CheckConstraint(
            "dest_lng     IS NULL OR (dest_lng     BETWEEN -180 AND 180)",
            name="ck_mission_dest_lng_range",
        ),
    )


# ---------- Normalizers ----------
@listens_for(DroneMission, "before_insert")
def _mission_before_insert(_m, _c, t: DroneMission) -> None:
    if t.drone_id:
        t.drone_id = t.drone_id.strip()[:80]
    if t.destination:
        t.destination = t.destination.strip()[:240]
    if t.failure_reason:
        t.failure_reason = t.failure_reason.strip()[:200]
    if t.notes:
        t.notes = t.notes.strip()

@listens_for(DroneMission, "before_update")
def _mission_before_update(_m, _c, t: DroneMission) -> None:
    _mission_before_insert(_m, _c, t)  # normalize the same way
