# backend/routes/greet_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Optional, Dict, List, Tuple

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field, ConfigDict

from backend.db import get_db  # kept for parity if you later need the DB here
from sqlalchemy.orm import Session

# Optional: use the logged-in user's stored language
try:
    from backend.auth import get_current_user
    from backend.models.user import User
except Exception:  # if auth isn't wired yet
    get_current_user = lambda: None  # type: ignore
    User = object  # type: ignore

router = APIRouter(prefix="/greet", tags=["Greet"])

# --- Minimal i18n store (replace with gettext/Babel later if you like) ---
SUPPORTED_LANGS = {"en", "sw"}
MESSAGES: Dict[str, Dict[str, str]] = {
    "welcome": {
        "en": "Hello, welcome to our platform!",
        "sw": "Habari, karibu kwenye jukwaa letu!",
    }
}

# --- Schemas ---
class GreetOut(BaseModel):
    message: str = Field(..., example="Hello, welcome to our platform!")
    language: str = Field(..., example="en")
    model_config = ConfigDict(from_attributes=True)


# --- Helpers ---
def _normalize_tag(tag: Optional[str]) -> Optional[str]:
    if not tag:
        return None
    t = tag.strip().replace("_", "-")
    parts = [p for p in t.split("-") if p]
    if not parts:
        return None
    lang = parts[0].lower()
    # We only keep base language (en, sw) for this simple store
    return lang

def _parse_accept_language(header: Optional[str]) -> List[Tuple[str, float]]:
    """
    Returns [(lang, q), ...] sorted by q desc.
    Example header: "en-US,en;q=0.9,sw;q=0.8"
    """
    if not header:
        return []
    items: List[Tuple[str, float]] = []
    for part in header.split(","):
        piece = part.strip()
        if not piece:
            continue
        q = 1.0
        if ";q=" in piece:
            try:
                piece, qval = piece.split(";q=", 1)
                q = float(qval)
            except Exception:
                q = 1.0
        lang = _normalize_tag(piece)
        if lang:
            items.append((lang, q))
    # Sort by quality descending
    items.sort(key=lambda x: x[1], reverse=True)
    return items

def _select_language(
    query_lang: Optional[str],
    cookie_lang: Optional[str],
    user_lang: Optional[str],
    accept_language: Optional[str],
    default: str = "en",
) -> str:
    # 1) explicit query
    for candidate in (_normalize_tag(query_lang), _normalize_tag(cookie_lang), _normalize_tag(user_lang)):
        if candidate and candidate in SUPPORTED_LANGS:
            return candidate
    # 2) accept-language header
    for cand, _q in _parse_accept_language(accept_language):
        if cand in SUPPORTED_LANGS:
            return cand
    # 3) fallback
    return default

def _t(key: str, lang: str) -> str:
    return MESSAGES.get(key, {}).get(lang) or MESSAGES.get(key, {}).get("en") or ""


# --- Route ---
@router.get("/", response_model=GreetOut, summary="Greet the user")
def greet(
    request: Request,
    db: Session = Depends(get_db),  # not used now; here for future extension
    lang: Optional[str] = Query(None, description="Language code (e.g., en, sw)"),
    accept_language: Optional[str] = Header(None, alias="Accept-Language"),
    current_user: Optional[User] = Depends(get_current_user),  # Optional if auth not wired
):
    # Pull possible sources
    cookie_lang = request.cookies.get("lang")
    user_lang = getattr(current_user, "language", None) if current_user else None

    # Pick a language
    chosen = _select_language(lang, cookie_lang, user_lang, accept_language, default="en")

    # Build response
    return GreetOut(message=_t("welcome", chosen), language=chosen)

