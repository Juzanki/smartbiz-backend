# backend/routes/language_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from sqlalchemy.orm import Session

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.schemas import LanguagePreferenceUpdate

router = APIRouter(tags=["Preferences"])

# ---- Config ----
# Allow both bare language codes and region-specific tags.
SUPPORTED_LANG_TAGS: Set[str] = {
    "en", "en-US", "sw", "sw-TZ", "fr", "pt", "de"
}
CANONICAL_MAP = {
    "en-us": "en-US",
    "sw-tz": "sw-TZ",
}

LANG_COOKIE_NAME = "lang"
LANG_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


# ---- Schemas ----
class LanguageUpdateOut(BaseModel):
    message: str = Field(..., example="Language updated")
    language: str = Field(..., description="Canonical language tag (e.g., en, en-US, sw, sw-TZ)")
    previous_language: Optional[str] = None
    changed: bool = True


# ---- Helpers ----
def _normalize_tag(tag: str) -> str:
    """
    Normalize a language tag to a simple BCP-47-like form:
    - Replace '_' with '-'
    - lowercase language, UPPERCASE region
    - apply canonical map (e.g., en-us -> en-US)
    Accepts: 'en', 'EN', 'en_us', 'en-US', 'sw', 'sw-tz', etc.
    """
    if not tag or not isinstance(tag, str):
        raise HTTPException(status_code=422, detail="Language is required")

    raw = tag.strip().replace("_", "-")
    parts = [p for p in raw.split("-") if p]
    if len(parts) == 0:
        raise HTTPException(status_code=422, detail="Invalid language tag")

    lang = parts[0].lower()
    region = None
    if len(parts) >= 2 and len(parts[1]) == 2 and parts[1].isalpha():
        region = parts[1].upper()

    norm = f"{lang}-{region}" if region else lang
    return CANONICAL_MAP.get(norm.lower(), norm)


def _validate_allowed(norm: str) -> None:
    if norm not in SUPPORTED_LANG_TAGS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported language. Allowed: {sorted(SUPPORTED_LANG_TAGS)}",
        )


# ---- Routes ----
@router.get("/language", response_model=LanguageUpdateOut, summary="Get current language preference")
def get_language(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current = getattr(current_user, "language", None)
    if not current:
        return LanguageUpdateOut(message="No language set", language="en", previous_language=None, changed=False)
    return LanguageUpdateOut(message="Language fetched", language=current, previous_language=None, changed=False)


@router.put("/language", response_model=LanguageUpdateOut, summary="Update language preference")
def update_language(
    payload: LanguagePreferenceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    # Optional QoL:
    set_cookie: bool = Query(True, description="Also set a 'lang' cookie for the client"),
    prefer: Optional[str] = Header(None, alias="Prefer", description="Use 'return=minimal' to get 204 on no-op"),
):
    """
    Update the authenticated user's language preference.

    - Validates and canonicalizes the language tag.
    - Restricts to an allowed set (`SUPPORTED_LANG_TAGS`).
    - If unchanged and client sends `Prefer: return=minimal`, returns **204 No Content**.
    - Optionally sets a `lang` cookie for web clients.
    """
    norm = _normalize_tag(payload.language)
    _validate_allowed(norm)

    prev = getattr(current_user, "language", None)

    # No change?
    if prev == norm:
        if prefer == "return=minimal":
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return LanguageUpdateOut(message="No change", language=norm, previous_language=prev, changed=False)

    # Persist
    current_user.language = norm
    db.commit()
    db.refresh(current_user)

    # Build response
    body = LanguageUpdateOut(message="Language updated", language=norm, previous_language=prev, changed=True)

    # Optionally set cookie
    if set_cookie:
        resp = JSONResponse(status_code=status.HTTP_200_OK, content=body.model_dump())
        resp.set_cookie(
            key=LANG_COOKIE_NAME,
            value=norm,
            max_age=LANG_COOKIE_MAX_AGE,
            path="/",
            samesite="lax",
            secure=False,  # set True if you're strictly on HTTPS
            httponly=False,  # language cookie is safe for JS reads
        )
        return resp

    return body

