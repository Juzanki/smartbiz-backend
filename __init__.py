# backend/auth/__init__.py
"""
Compatibility shim so legacy imports keep working, e.g.:
    from backend.auth import get_current_user
The real implementation lives in backend.dependencies.authz.
"""

from __future__ import annotations
import os

# Keep a single source for tokenUrl, matching your routes
AUTH_TOKEN_URL: str = (os.getenv("AUTH_TOKEN_URL", "/auth/login") or "/auth/login").strip()

# Re-export the actual dependencies and guards
from backend.dependencies.authz import (  # noqa: E402
    oauth2_scheme,
    get_bearer_token,
    get_current_user,
    require_roles,
    require_scopes,
    owner_router,
    secure_router,
)

__all__ = [
    "AUTH_TOKEN_URL",
    "oauth2_scheme",
    "get_bearer_token",
    "get_current_user",
    "require_roles",
    "require_scopes",
    "owner_router",
    "secure_router",
]
