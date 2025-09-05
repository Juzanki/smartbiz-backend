# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Optional
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from functools import lru_cache
import time as pytime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, and_
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.env_ai import get_weather_by_location
from backend.models.product import Product
router = APIRouter()

# ---- Config ----
TZ = ZoneInfo("Africa/Dar_es_Salaam")
WEATHER_TTL_SECONDS = 10 * 60  # cache kwa 10 dakika

class City(BaseModel):
    city: str
    lat: float
    lng: float

DEFAULT_CITIES: List[City] = [
    City(city="Dar es Salaam", lat=-6.8, lng=39.28),
    City(city="Dodoma",        lat=-6.2, lng=35.75),
    City(city="Moshi",         lat=-3.35, lng=37.33),
    City(city="Arusha",        lat=-3.37, lng=36.68),
    City(city="Mbeya",         lat=-8.9, lng=33.45),
]

class ZoneForecast(BaseModel):
    city: str
    lat: float
    lng: float
    weather: str = Field(..., description="sunny | cloudy | rainy | stormy | windy | other")
    recommendation: str
    products: List[str] = []
    as_of: str = Field(..., description="Local time in HH:MM (Africa/Dar_es_Salaam)")

# ---- Simple TTL cache for weather ----
_weather_cache: dict[str, tuple[float, dict]] = {}

def _get_weather_cached(city: str) -> dict:
    now = pytime.time()
    key = city.lower().strip()
    cached = _weather_cache.get(key)
    if cached and (now - cached[0] < WEATHER_TTL_SECONDS):
        return cached[1]
    data = get_weather_by_location(city)
    # force expected shape
    if not isinstance(data, dict) or "weather" not in data:
        data = {"weather": "unknown"}
    _weather_cache[key] = (now, data)
    return data

def _normalize_weather(raw: str) -> str:
    c = (raw or "").lower().strip()
    if any(k in c for k in ("thunder", "storm", "lightning")):
        return "stormy"
    if any(k in c for k in ("rain", "drizzle", "shower")):
        return "rainy"
    if any(k in c for k in ("clear", "sun")):
        return "sunny"
    if "wind" in c or "breez" in c:
        return "windy"
    if any(k in c for k in ("cloud", "overcast", "mist", "haze")):
        return "cloudy"
    return "other"

def _time_filters(model, now_t: dtime):
    """
    Hurejesha filter ya SQLAlchemy inayoruhusu:
    - Kipindi cha kawaida: start <= now <= end
    - Kipindi kinachovuka usiku: start > end, basi (now >= start) OR (now <= end)
    """
    s = model.preferred_time_start
    e = model.preferred_time_end
    return or_(
        and_(s <= now_t, e >= now_t),       # kawaida
        and_(s > e, or_(now_t >= s, now_t <= e))  # kinavuka usiku
    )

def _product_names(products: List[Product]) -> List[str]:
    try:
        return [p.name for p in products]
    except Exception:
        return []

@router.get("/forecast/zones", response_model=List[ZoneForecast])
def forecast_zones(
    db: Session = Depends(get_db),
    limit: int = Query(2, ge=0, le=10, description="Max products per city"),
    cities: Optional[str] = Query(
        None,
        description="Optional CSV ya majina ya miji, ikitolewa itatumika badala ya default list."
    ),
):
    """
    Hutoa hali ya hewa ya miji + mapendekezo ya bidhaa kulingana na:
    - weather_type (mf. 'sunny','rainy','cloudy','stormy','windy','other' au 'any')
    - preferred_location (ILIKE %city%)
    - preferred_time window (ikiwemo kuvuka usiku)
    """
    try:
        local_now = datetime.now(TZ)
        now_label = local_now.strftime("%H:%M")
        now_t: dtime = local_now.time()

        # Resolve cities
        city_objs: List[City]
        if cities:
            # Parse CSV into City entries with only 'city' field (lat/lng unknown)
            names = [c.strip() for c in cities.split(",") if c.strip()]
            # tunabaki na lat/lng = 0.0 kama placeholder (unaweza kuboresha kwa geocoder baadaye)
            city_objs = [City(city=n, lat=0.0, lng=0.0) for n in names] if names else DEFAULT_CITIES
        else:
            city_objs = DEFAULT_CITIES

        out: List[ZoneForecast] = []

        for c in city_objs:
            weather_raw = _get_weather_cached(c.city).get("weather", "unknown")
            weather = _normalize_weather(weather_raw)

            # 1) Jaribu: weather match + location + time window
            q = db.query(Product).filter(
                or_(Product.weather_type == weather, Product.weather_type == "any"),
                Product.preferred_location.ilike(f"%{c.city}%"),
                _time_filters(Product, now_t)
            )
            products = q.limit(limit).all() if limit > 0 else []

            # 2) Fallback kama hakuna kitu kwa location â€” jaribu bila location
            if not products and limit > 0:
                q2 = db.query(Product).filter(
                    or_(Product.weather_type == weather, Product.weather_type == "any"),
                    _time_filters(Product, now_t)
                )
                products = q2.limit(limit).all()

            names = _product_names(products)
            recommendation = ", ".join(names) if names else "None"

            out.append(ZoneForecast(
                city=c.city,
                lat=c.lat,
                lng=c.lng,
                weather=weather,
                recommendation=recommendation,
                products=names,
                as_of=now_label
            ))

        return out

    except Exception as exc:
        # Epuka kuvujisha maelezo ya ndani kwa client
        raise HTTPException(status_code=500, detail="Failed to build forecast zones") from exc

