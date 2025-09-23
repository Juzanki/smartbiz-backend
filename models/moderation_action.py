# backend/models/moderation_action.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Robust re-export for ModerationAction.

- Hushughulikia tofauti za majina ya faili: moderation.py / moderation_action.py / moderationaction.py
- Hupunguza makosa ya mapper timing/circular import kwa kutumia uleteji unaocheleweshwa (lazy-ish).
- Inahakikisha `from backend.models.moderation_action import ModerationAction` inafanya kazi bila kujali
  jina halisi la faili lenye model.
"""

from importlib import import_module
from typing import TYPE_CHECKING

__all__ = ["ModerationAction"]

_CANDIDATES = (
    # Relative ndani ya package hii
    ".moderation",
    ".moderation_action",
    ".moderationaction",
    # Majina kamili (fallback kama relative haipatikani)
    "backend.models.moderation",
    "backend.models.moderation_action",
    "backend.models.moderationaction",
)

if TYPE_CHECKING:
    # Wakati wa type checking, tunatumia njia ya moja kwa moja (haitekelezwi runtime)
    from .moderation import ModerationAction  # type: ignore
else:
    def _load_moderation_action():
        last_err = None
        for mod_name in _CANDIDATES:
            try:
                mod = (
                    import_module(mod_name, package=__package__)
                    if mod_name.startswith(".")
                    else import_module(mod_name)
                )
                cls = getattr(mod, "ModerationAction", None)
                if cls is not None:
                    return cls
            except Exception as e:  #endelea kujaribu wengine
                last_err = e
        raise ModuleNotFoundError(
            "Could not locate 'ModerationAction' in any of: "
            + ", ".join(_CANDIDATES)
        ) from last_err

    # Leta darasa halisi mara moja (imejaribiwa na candidates zote)
    ModerationAction = _load_moderation_action()  # type: ignore
