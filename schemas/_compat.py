# backend/schemas/_compat.py
# -*- coding: utf-8 -*-
"""
Pydantic compatibility layer for v1/v2.

This module ensures schema definitions work regardless of whether the
environment uses Pydantic v1.x or v2.x, by:
    - Exporting a consistent set of imports (BaseModel, Field, ConfigDict, EmailStr, etc.)
    - Providing a unified `field_validator` decorator for both versions
    - Offering a no-op `ConfigDict` in v1 (since it does not exist there)
    - Setting the `P2` flag to True when running under Pydantic v2

Usage in schemas:
    from ._compat import BaseModel, Field, ConfigDict, field_validator, P2
"""

from __future__ import annotations

try:
    # ---- Pydantic v2 imports ----
    from pydantic import BaseModel, Field, ConfigDict, EmailStr, field_validator
    P2 = True

except ImportError:
    # ---- Pydantic v1 fallback ----
    from pydantic import BaseModel, Field, EmailStr  # type: ignore
    from pydantic import validator as _validator    # type: ignore

    P2 = False
    ConfigDict = dict  # type: ignore

    # Shim for v1 -> v2-style field_validator
    def field_validator(field_name: str, *, mode: str = "after"):
        """
        v1-compatible decorator that mimics Pydantic v2's `field_validator`.

        Example:
            @field_validator("name", mode="before")
            def check_name(cls, v):
                return v
        """
        pre = mode == "before"

        def deco(fn):
            return _validator(field_name, pre=pre, allow_reuse=True)(fn)

        return deco

__all__ = [
    "P2",            # True if using Pydantic v2
    "BaseModel",
    "Field",
    "ConfigDict",
    "EmailStr",
    "field_validator",
]
