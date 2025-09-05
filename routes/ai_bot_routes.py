from __future__ import annotations
from backend.schemas.user import UserOut
# backend/routes/ai_bot.py
import hashlib
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Header, Response, Request
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models.ai_bot_settings import AIBotSettings
from backend.models.user import User as UserModel
from backend.dependencies import get_current_user

# Jaribu kuchukua schema yako; kama haipo, tutatengeneza ya muda (compat v1/v2)
try:
    from backend.schemas import AIBotSettingsSchema as AIBotSettingsOut  # response model
except Exception:  # pragma: no cover
    from pydantic import BaseModel, Field
    class AIBotSettingsOut(BaseModel):  # fallback minimal
        id: Optional[int] = None
        user_id: int
        model: Optional[str] = None
        language: str = "sw"
        temperature: float = 0.7
        max_tokens: int = 800
        system_prompt: Optional[str] = None
        persona: Optional[str] = None
        stream: Optional[bool] = True
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None

# ===== Router =====
router = APIRouter(prefix="/ai-bot", tags=["AI Bot"])

# ===== Helpers =====
IMMUTABLE_FIELDS = {"id", "user_id", "created_at", "updated_at"}

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _to_dict(payload) -> Dict[str, Any]:
    # pydantic v2 / v1 friendly
    if hasattr(payload, "model_dump"):
        return payload.model_dump(exclude_unset=True)
    if hasattr(payload, "dict"):
        return payload.dict(exclude_unset=True)
    return dict(payload)

def _normalize(data: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(data)
    # language
    if "language" in out and isinstance(out["language"], str):
        out["language"] = out["language"].strip().lower().replace("_", "-") or "sw"
    # persona
    if "persona" in out and isinstance(out["persona"], str):
        out["persona"] = out["persona"].strip()
    # temperature
    if "temperature" in out and out["temperature"] is not None:
        try:
            t = float(out["temperature"])
            out["temperature"] = max(0.0, min(t, 2.0))
        except Exception:
            out["temperature"] = 0.7
    # max_tokens
    if "max_tokens" in out and out["max_tokens"] is not None:
        try:
            m = int(out["max_tokens"])
            out["max_tokens"] = max(32, min(m, 4000))
        except Exception:
            out["max_tokens"] = 800
    # model
    if "model" in out and isinstance(out["model"], str):
        out["model"] = out["model"].strip()
    return out

def _filter_assignable(instance: AIBotSettings, data: Dict[str, Any]) -> Dict[str, Any]:
    """Weka tu fields ambazo zipo kwenye model na si immutable."""
    assignable = {}
    for k, v in data.items():
        if k in IMMUTABLE_FIELDS:
            continue
        # kwa declarative model, hasattr(instance, k) ni salama
        if hasattr(instance, k):
            assignable[k] = v
    return assignable

def _compute_etag(obj: AIBotSettings) -> str:
    """
    ETag kwa concurrency/caching: tumia updated_at ikiwa ipo, vinginevyo hash ya subset ya fields.
    """
    if hasattr(obj, "updated_at") and getattr(obj, "updated_at", None):
        base = str(int(obj.updated_at.replace(tzinfo=timezone.utc).timestamp()))
    else:
        raw = "|".join(
            str(getattr(obj, a, None))
            for a in ("id", "user_id", "model", "language", "temperature", "max_tokens", "persona")
        )
        base = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f'W/"{base}"'

def _apply_no_store_headers(resp: Response, etag: Optional[str] = None) -> None:
    resp.headers["Cache-Control"] = "no-store"
    if etag:
        resp.headers["ETag"] = etag

# ===== Defaults (utabadilisha kadri ya .env yako) =====
DEFAULTS = {
    "language": "sw",
    "temperature": 0.7,
    "max_tokens": 800,
    "stream": True,
}

# ========================= GET (autocreate option) =========================
@router.get("/settings", response_model=AIBotSettingsOut)
def get_ai_bot_settings(
    response: Response,
    autocreate: bool = Query(True, description="Auto-create with defaults if missing"),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    settings = (
        db.query(AIBotSettings)
        .filter(AIBotSettings.user_id == current_user.id)
        .first()
    )
    created = False
    if not settings and autocreate:
        settings = AIBotSettings(user_id=current_user.id, **DEFAULTS)  # type: ignore[arg-type]
        if hasattr(settings, "created_at"):
            settings.created_at = _utcnow()  # type: ignore[attr-defined]
        if hasattr(settings, "updated_at"):
            settings.updated_at = _utcnow()  # type: ignore[attr-defined]
        db.add(settings)
        try:
            db.commit()
            db.refresh(settings)
            created = True
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="Settings already exist.")
        except Exception:
            db.rollback()
            raise HTTPException(status_code=500, detail="Failed to create defaults.")
    elif not settings:
        raise HTTPException(status_code=404, detail="No settings found")

    etag = _compute_etag(settings)
    _apply_no_store_headers(response, etag=etag)
    if created:
        response.status_code = status.HTTP_201_CREATED
    return settings  # FastAPI will coerce to AIBotSettingsOut

# ========================= PUT (upsert + concurrency) ======================
@router.put("/settings", response_model=AIBotSettingsOut)
def upsert_ai_bot_settings(
    payload: AIBotSettingsOut,  # au schema yako ya input inayofanana
    response: Response,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    if_match: Optional[str] = Query(None, description='ETag from previous GET (or use "If-Match" header)'),
    if_match_hdr: Optional[str] = Header(None, alias="If-Match"),
):
    """
    Upsert (weka zote). Ukiweka `If-Match` au `?if_match=` tunazuia **lost updates**.
    """
    cond = if_match or if_match_hdr

    settings = (
        db.query(AIBotSettings)
        .filter(AIBotSettings.user_id == current_user.id)
        .first()
    )

    body = _normalize(_to_dict(payload))

    # create
    if not settings:
        settings = AIBotSettings(user_id=current_user.id, **_filter_assignable(AIBotSettings(), body))  # type: ignore
        if hasattr(settings, "created_at"):
            settings.created_at = _utcnow()  # type: ignore[attr-defined]
        if hasattr(settings, "updated_at"):
            settings.updated_at = _utcnow()  # type: ignore[attr-defined]
        db.add(settings)
        try:
            db.commit()
            db.refresh(settings)
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="Settings already exist.")
        except Exception:
            db.rollback()
            raise HTTPException(status_code=500, detail="Failed to create settings.")
        etag = _compute_etag(settings)
        _apply_no_store_headers(response, etag=etag)
        response.status_code = status.HTTP_201_CREATED
        return settings

    # update all (concurrency check)
    current_etag = _compute_etag(settings)
    if cond and cond != current_etag:
        raise HTTPException(status_code=412, detail="Precondition failed (ETag mismatch).")

    for k, v in _filter_assignable(settings, body).items():
        setattr(settings, k, v)
    if hasattr(settings, "updated_at"):
        settings.updated_at = _utcnow()  # type: ignore[attr-defined]

    try:
        db.commit()
        db.refresh(settings)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update settings.")

    etag = _compute_etag(settings)
    _apply_no_store_headers(response, etag=etag)
    return settings

# ========================= PATCH (partial update) ==========================
@router.patch("/settings", response_model=AIBotSettingsOut)
def patch_ai_bot_settings(
    payload: Dict[str, Any],
    response: Response,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    if_match: Optional[str] = Query(None),
    if_match_hdr: Optional[str] = Header(None, alias="If-Match"),
):
    """
    Partial update (only fields provided). Inapenda mobile clients.
    """
    cond = if_match or if_match_hdr

    settings = (
        db.query(AIBotSettings)
        .filter(AIBotSettings.user_id == current_user.id)
        .first()
    )
    if not settings:
        raise HTTPException(status_code=404, detail="No settings found")

    current_etag = _compute_etag(settings)
    if cond and cond != current_etag:
        raise HTTPException(status_code=412, detail="Precondition failed (ETag mismatch).")

    body = _normalize(_to_dict(payload))
    for k, v in _filter_assignable(settings, body).items():
        setattr(settings, k, v)
    if hasattr(settings, "updated_at"):
        settings.updated_at = _utcnow()  # type: ignore[attr-defined]

    try:
        db.commit()
        db.refresh(settings)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update settings.")

    etag = _compute_etag(settings)
    _apply_no_store_headers(response, etag=etag)
    return settings



