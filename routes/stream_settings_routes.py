# backend/routes/stream_settings.py
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Stream Settings API (mobile-first, international-ready)

Goals:
- Clean English-only code & docs
- Minimal payloads (great for mobile)
- Expanded functionality with safe, optional fallbacks to your current CRUD
- Concurrency-friendly update shape (optimistic If-Match support if your model has a version field)

Routes:
- GET    /stream-settings/{stream_id}                      -> fetch (optionally create if missing)
- GET    /stream-settings/{stream_id}/effective            -> fetch effective settings (defaults + overrides)
- GET    /stream-settings/template                         -> fetch default/template settings
- PUT    /stream-settings/{stream_id}                      -> replace (upsert)
- PATCH  /stream-settings/{stream_id}                      -> partial update (merge, mobile-friendly)
- POST   /stream-settings/{stream_id}/toggle               -> quick boolean toggle (key/value)
- DELETE /stream-settings/{stream_id}                      -> reset/delete settings (fallback to defaults)

Notes:
- This file gracefully tries extra crud helpers if you have them; otherwise it falls back
  to your existing `create_or_update_settings` and `get_settings`.
"""

from typing import Optional, Dict, Any
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Header,
    status,
)
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.auth import get_current_user  # optional: enforce per-stream ownership
from backend.models.user import User
from backend.schemas.stream_settings_schemas import (
    StreamSettingsUpdate,
    StreamSettingsOut,
)
from backend.crud import stream_settings_crud

router = APIRouter(prefix="/stream-settings", tags=["Stream Settings"])


# ---------- helpers ----------
def _dump_partial(model: StreamSettingsUpdate) -> Dict[str, Any]:
    """
    Return only fields provided by client (Pydantic v2/v1 compatible).
    """
    try:
        # pydantic v2
        return model.model_dump(exclude_unset=True)
    except AttributeError:
        # pydantic v1
        return model.dict(exclude_unset=True)


def _assert_can_manage(db: Session, user: User, stream_id: int) -> None:
    """
    Optional ownership/permissions guard. If you implement a helper in CRUD,
    it will be used; otherwise it no-ops (keeps current behavior).
    """
    guard = getattr(stream_settings_crud, "user_can_manage_stream", None)
    if callable(guard):
        allowed = guard(db, user, stream_id)
        if not allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed for this stream")


def _get_defaults(db: Session) -> Dict[str, Any]:
    """
    Ask CRUD for defaults if available; otherwise return an empty dict.
    """
    func = getattr(stream_settings_crud, "get_default_settings", None)
    if callable(func):
        return func(db) or {}
    return {}


def _get_effective(db: Session, stream_id: int) -> Dict[str, Any]:
    """
    Effective = defaults + overrides. Prefer CRUD's computation if available.
    """
    func = getattr(stream_settings_crud, "get_effective_settings", None)
    if callable(func):
        eff = func(db, stream_id)
        if eff:
            return eff
    # fallback: shallow merge
    defaults = _get_defaults(db)
    current = stream_settings_crud.get_settings(db, stream_id) or {}
    # If current is a model instance, coerce to dict via schema
    if isinstance(current, dict):
        overrides = current
    else:
        try:
            # If your crud returns ORM; rely on your schema to serialize
            overrides = StreamSettingsOut.model_validate(current).model_dump()
        except Exception:
            overrides = {}
    return {**defaults, **overrides}


# ---------- routes ----------

@router.get(
    "/{stream_id}",
    response_model=StreamSettingsOut,
    summary="Get stream settings (optionally create if missing)",
)
def get_stream_settings(
    stream_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    create_if_missing: bool = Query(False, description="Create settings with defaults if none exist"),
):
    _assert_can_manage(db, current_user, stream_id)

    settings = stream_settings_crud.get_settings(db, stream_id)
    if not settings:
        if not create_if_missing:
            raise HTTPException(status_code=404, detail="Settings not found")
        # Upsert with defaults
        defaults = _get_defaults(db)
        update = StreamSettingsUpdate(**defaults) if defaults else StreamSettingsUpdate()
        settings = stream_settings_crud.create_or_update_settings(db, stream_id, update)
    return settings


@router.get(
    "/{stream_id}/effective",
    response_model=StreamSettingsOut,
    summary="Get effective settings (defaults + overrides)",
)
def get_effective_stream_settings(
    stream_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_can_manage(db, current_user, stream_id)
    data = _get_effective(db, stream_id)
    # ensure it validates to Out schema
    return StreamSettingsOut(**data)


@router.get(
    "/template",
    response_model=StreamSettingsOut,
    summary="Get default/template settings",
)
def get_settings_template(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    defaults = _get_defaults(db)
    return StreamSettingsOut(**defaults) if defaults else StreamSettingsOut()


@router.put(
    "/{stream_id}",
    response_model=StreamSettingsOut,
    status_code=status.HTTP_201_CREATED,
    summary="Replace (upsert) stream settings",
)
def replace_stream_settings(
    stream_id: int,
    data: StreamSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Query(
        None, description="Optional idempotency key if supported in CRUD/model"
    ),
    if_match: Optional[str] = Header(
        None, convert_underscores=False,
        description="Optional concurrency token (e.g., version) for optimistic updates"
    ),
):
    """
    Replace the entire settings object (mobile-friendly upsert).
    If your model supports `version` or `updated_at`, your CRUD can
    validate `if_match` to prevent lost updates.
    """
    _assert_can_manage(db, current_user, stream_id)

    # Prefer a specialized crud method if available
    upsert = getattr(stream_settings_crud, "replace_settings", None)
    if callable(upsert):
        return upsert(db, stream_id, data, idempotency_key=idempotency_key, if_match=if_match)
    # Fallback to existing create_or_update
    return stream_settings_crud.create_or_update_settings(db, stream_id, data)


@router.patch(
    "/{stream_id}",
    response_model=StreamSettingsOut,
    summary="Partially update stream settings (merge)",
)
def patch_stream_settings(
    stream_id: int,
    data: StreamSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    create_if_missing: bool = Query(True, description="Create with defaults if not found"),
    if_match: Optional[str] = Header(
        None, convert_underscores=False,
        description="Optional concurrency token (e.g., version) for optimistic updates"
    ),
):
    """
    Mobile-first partial update: only send fields you changed.
    - Merges with existing settings
    - Optionally creates with defaults if missing
    """
    _assert_can_manage(db, current_user, stream_id)

    # If you have a dedicated partial update in CRUD, prefer that
    partial = getattr(stream_settings_crud, "partial_update_settings", None)
    if callable(partial):
        return partial(db, stream_id, data, create_if_missing=create_if_missing, if_match=if_match)

    # Generic merge fallback
    current = stream_settings_crud.get_settings(db, stream_id)
    if not current:
        if not create_if_missing:
            raise HTTPException(status_code=404, detail="Settings not found")
        base = _get_defaults(db)
    else:
        try:
            base = StreamSettingsOut.model_validate(current).model_dump()
        except Exception:
            # last-resort: assume dict-like
            base = dict(current) if isinstance(current, dict) else {}

    changes = _dump_partial(data)
    merged: Dict[str, Any] = {**base, **changes}

    return stream_settings_crud.create_or_update_settings(
        db, stream_id, StreamSettingsUpdate(**merged)
    )


@router.post(
    "/{stream_id}/toggle",
    response_model=StreamSettingsOut,
    summary="Quick boolean toggle for a single setting key",
)
def toggle_stream_setting(
    stream_id: int,
    key: str = Query(..., min_length=1, description="Setting field name to toggle"),
    value: bool = Query(..., description="Boolean value to set"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Designed for fast mobile UX where you toggle one switch at a time.
    """
    _assert_can_manage(db, current_user, stream_id)

    # If CRUD exposes a dedicated toggle, use it
    toggle_fn = getattr(stream_settings_crud, "toggle_setting", None)
    if callable(toggle_fn):
        return toggle_fn(db, stream_id, key, value)

    # Fallback: generic patch
    data = StreamSettingsUpdate(**{key: value})
    current = stream_settings_crud.get_settings(db, stream_id)
    if not current:
        base = _get_defaults(db)
    else:
        try:
            base = StreamSettingsOut.model_validate(current).model_dump()
        except Exception:
            base = dict(current) if isinstance(current, dict) else {}
    merged = {**base, **_dump_partial(data)}
    return stream_settings_crud.create_or_update_settings(db, stream_id, StreamSettingsUpdate(**merged))


@router.delete(
    "/{stream_id}",
    response_model=StreamSettingsOut,
    summary="Reset or delete settings; returns defaults",
)
def reset_stream_settings(
    stream_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    hard_delete: bool = Query(False, description="If true and supported, remove row instead of resetting"),
):
    """
    Reset the stream settings to defaults and return the new effective state.
    """
    _assert_can_manage(db, current_user, stream_id)

    # Prefer a dedicated reset/delete in CRUD
    reset_fn = getattr(stream_settings_crud, "reset_settings", None)
    delete_fn = getattr(stream_settings_crud, "delete_settings", None)

    if hard_delete and callable(delete_fn):
        delete_fn(db, stream_id)

    elif callable(reset_fn):
        reset_fn(db, stream_id)

    else:
        # Fallback: replace with defaults via upsert
        defaults = _get_defaults(db)
        update = StreamSettingsUpdate(**defaults) if defaults else StreamSettingsUpdate()
        stream_settings_crud.create_or_update_settings(db, stream_id, update)

    # Return current effective state after reset
    data = _get_effective(db, stream_id)
    return StreamSettingsOut(**data)

