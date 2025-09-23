# backend/middleware/cors.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Strict CORS middleware for Render (backend) + Netlify/Render (frontend).

Highlights
- No localhost defaults.
- Exact allowlist from env (strongly recommended).
- Optional regex for Netlify deploy previews of YOUR site only.
- Credentials-safe (no "*" when allow_credentials=True).
- Sensible defaults for methods/headers/exposed headers.

Environment variables (examples):
  FRONTEND_URL=https://your-site.netlify.app
  FRONTEND_URLS=https://your-custom-domain.com, https://your-frontend.onrender.com
  NETLIFY_SITE_HOST=your-site.netlify.app          # base host of your Netlify site
  NETLIFY_ALLOW_PREVIEWS=true                      # also allow https://<hash>--your-site.netlify.app
  CORS_ALLOW_HEADERS=Authorization,Content-Type
  CORS_EXPOSE_HEADERS=Content-Disposition,Link,Location,X-Request-ID
  CORS_MAX_AGE=86400

Usage:
    from fastapi import FastAPI
    from backend.middleware.cors import add_cors

    app = FastAPI()
    add_cors(app)  # reads env + (optionally) add_cors(app, extra_origins={"https://..."} )
"""

import os
import re
from typing import Iterable, Optional, Set, List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


# --------------------------- helpers ---------------------------

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

def _is_true(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in _TRUTHY

def _split_env_list(value: str) -> List[str]:
    if not value:
        return []
    # split by comma or whitespace
    parts = re.split(r"[,\s]+", value.strip())
    return [p for p in (s.strip() for s in parts) if p]

def _normalize_origin(origin: str) -> Optional[str]:
    """
    Return a normalized origin string (scheme + host[, :port]) without trailing slash.
    Only https/http origins are accepted.
    """
    if not origin:
        return None
    o = origin.strip().rstrip("/")
    if not o:
        return None
    if not (o.startswith("https://") or o.startswith("http://")):
        # assume https for hosted frontends
        o = "https://" + o
    return o

def _collect_exact_origins(extra_origins: Optional[Iterable[str]] = None) -> Set[str]:
    """Collect exact origins from environment + optional extras."""
    allowed: Set[str] = set()

    # Single URL entries
    for key in ("FRONTEND_URL", "PUBLIC_FRONTEND_URL", "SITE_URL", "RENDER_FRONTEND_URL", "NETLIFY_BASE_URL"):
        v = _normalize_origin(os.getenv(key, ""))
        if v:
            allowed.add(v)

    # List entries
    for key in ("FRONTEND_URLS", "CORS_ALLOWED_ORIGINS"):
        raw = os.getenv(key, "")
        for item in _split_env_list(raw):
            v = _normalize_origin(item)
            if v:
                allowed.add(v)

    # Programmatic extras
    for item in (extra_origins or []):
        v = _normalize_origin(item)
        if v:
            allowed.add(v)

    # Optional explicit Netlify base host (e.g., "your-site.netlify.app")
    base_host = os.getenv("NETLIFY_SITE_HOST", "").strip().rstrip("/")
    if base_host:
        exact = _normalize_origin(base_host)
        if exact:
            allowed.add(exact)

    # Optional explicit Render frontend host (e.g., "your-frontend.onrender.com")
    render_host = os.getenv("RENDER_FRONTEND_HOST", "").strip().rstrip("/")
    if render_host:
        exact = _normalize_origin(render_host)
        if exact:
            allowed.add(exact)

    return allowed

def _build_netlify_preview_regex() -> Optional[str]:
    """
    Build a regex that ONLY allows deploy-preview subdomains for your site:
      https://<preview>--<site>.netlify.app
    Requires NETLIFY_SITE_HOST=<site>.netlify.app and NETLIFY_ALLOW_PREVIEWS=true.
    """
    if not _is_true(os.getenv("NETLIFY_ALLOW_PREVIEWS")):
        return None

    host = os.getenv("NETLIFY_SITE_HOST", "").strip().lower()
    if not host or not host.endswith(".netlify.app"):
        return None

    # host like "your-site.netlify.app"
    site = re.escape(host.replace(".netlify.app", ""))
    # allow https://<any>--your-site.netlify.app
    pattern = rf"^https://[a-z0-9-]+--{site}\.netlify\.app$"
    return pattern

def _combine_regexes(*patterns: Optional[str]) -> Optional[str]:
    pats = [p for p in patterns if p]
    if not pats:
        return None
    if len(pats) == 1:
        return pats[0]
    # Join into a single non-capturing group, all fully anchored already
    inner = "|".join(pats)
    return rf"(?:{inner})"


# --------------------------- main API ---------------------------

def add_cors(app: FastAPI, *, extra_origins: Optional[Iterable[str]] = None) -> None:
    """
    Register a strict CORS policy for production (Netlify/Render frontends).
    - No localhost.
    - Exact allowlist from env.
    - Optional Netlify preview regex for your site only.
    """

    # Exact origins
    exact_origins = sorted(_collect_exact_origins(extra_origins))

    # Optional regexes
    regex_from_env = os.getenv("CORS_ALLOW_REGEX", "").strip() or None
    netlify_preview_regex = _build_netlify_preview_regex()

    allow_origin_regex = _combine_regexes(regex_from_env, netlify_preview_regex)

    # Credentials are typically required (Authorization header)
    allow_credentials = _is_true(os.getenv("CORS_ALLOW_CREDENTIALS", "1"))

    # Safety: when credentials=True, do NOT use "*" origins.
    # We require either exact origins or an explicit regex.
    if allow_credentials and not (exact_origins or allow_origin_regex):
        raise RuntimeError(
            "CORS is too strict: no allowed origins configured. "
            "Set FRONTEND_URL/FRONTEND_URLS (or NETLIFY_SITE_HOST) and/or CORS_ALLOW_REGEX."
        )

    # Methods & headers
    allow_methods = _split_env_list(os.getenv("CORS_ALLOW_METHODS", "")) or ["*"]
    allow_headers = _split_env_list(os.getenv("CORS_ALLOW_HEADERS", "")) or ["*"]

    # Exposed headers
    expose_headers = _split_env_list(os.getenv("CORS_EXPOSE_HEADERS", "")) or [
        "Content-Disposition",
        "Link",
        "Location",
        "X-Request-ID",
    ]

    max_age = int(os.getenv("CORS_MAX_AGE", "86400") or 86400)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=exact_origins,     # exact hosts only
        allow_origin_regex=allow_origin_regex,  # optional: Netlify previews or custom regex
        allow_credentials=allow_credentials,
        allow_methods=allow_methods,
        allow_headers=allow_headers,
        expose_headers=expose_headers,
        max_age=max_age,
    )

__all__ = ["add_cors"]
