# backend/schemas/__init__.py
# -*- coding: utf-8 -*-
"""
Unified, safe re-exports for Pydantic schemas.

- Works with Pydantic v2 (ConfigDict) and v1 (class Config/orm_mode).
- Provides lazy imports so routes can `from backend.schemas import X`.
- Exposes certain submodules (e.g. `balance_schemas`) for compatibility.
"""

from __future__ import annotations

from importlib import import_module
from contextlib import suppress
from typing import Any, Optional, Dict, List

# ---------------- Detect Pydantic v2 vs v1 ----------------
try:
    from pydantic import BaseModel, ConfigDict  # v2
    _V2 = True
except Exception:  # v1 fallback
    from pydantic import BaseModel  # type: ignore
    _V2 = False


# ---------------- Minimal placeholder (v1/v2-safe) ----------------
if _V2:
    class _PlaceholderModel(BaseModel):  # type: ignore
        model_config = ConfigDict(extra="allow")
else:
    class _PlaceholderModel(BaseModel):  # type: ignore
        class Config:  # type: ignore
            extra = "allow"


# ---------------- Helpers ----------------
def _maybe_export(module: str, candidates: List[str], as_name: str,
                  fallback: Optional[Any] = None) -> bool:
    """
    Try to import the first symbol found in `candidates` from
    `backend.schemas.<module>`; expose as globals()[as_name].
    If all fail and `fallback` is provided, expose that.
    """
    full_mod = f"{__name__}.{module}"  # e.g. backend.schemas.user
    with suppress(Exception):
        mod = import_module(full_mod)
        for sym in candidates:
            if hasattr(mod, sym):
                globals()[as_name] = getattr(mod, sym)
                return True
    if fallback is not None:
        globals()[as_name] = fallback
        return True
    return False


def _expose_module(module: str, as_name: Optional[str] = None) -> bool:
    """
    Expose a submodule so `from backend.schemas import <module>` works.
    """
    full_mod = f"{__name__}.{module}"
    with suppress(Exception):
        mod = import_module(full_mod)
        globals()[as_name or module] = mod
        return True
    return False


# ---------------- USER ----------------
_maybe_export("user", ["UserOut", "User", "UserRead"], "User", fallback=_PlaceholderModel)
_maybe_export("user", ["UserCreate"], "UserCreate", fallback=_PlaceholderModel)
_maybe_export("user", ["UserUpdate"], "UserUpdate", fallback=_PlaceholderModel)

# ---------------- AUTH ----------------
if not _maybe_export("auth", ["Token"], "Token"):
    if _V2:
        class Token(BaseModel):  # type: ignore
            access_token: str
            token_type: str = "bearer"
            expires_in: Optional[int] = None  # type: ignore
            model_config = ConfigDict(extra="ignore")
    else:
        class Token(BaseModel):  # type: ignore
            access_token: str
            token_type: str = "bearer"
            expires_in: Optional[int] = None  # type: ignore
            class Config:  # type: ignore
                extra = "ignore"

if not _maybe_export("auth", ["TokenData"], "TokenData"):
    if _V2:
        class TokenData(BaseModel):  # type: ignore
            sub: Optional[str] = None  # type: ignore
            scopes: List[str] = []     # type: ignore
            model_config = ConfigDict(extra="ignore")
    else:
        class TokenData(BaseModel):  # type: ignore
            sub: Optional[str] = None  # type: ignore
            scopes: List[str] = []     # type: ignore
            class Config:  # type: ignore
                extra = "ignore"

# ---------------- POST ----------------
_maybe_export("post", ["PostCreate"], "PostCreate", fallback=_PlaceholderModel)
_maybe_export("post", ["PostOut", "PostRead"], "PostOut", fallback=_PlaceholderModel)

# ---------------- FORGOT PASSWORD -----
_maybe_export("forgot_password", ["ForgotPasswordRequest"], "ForgotPasswordRequest", fallback=_PlaceholderModel)
_maybe_export("forgot_password", ["VerifyResetCode"], "VerifyResetCode", fallback=_PlaceholderModel)
_maybe_export("forgot_password", ["ResetPassword"], "ResetPassword", fallback=_PlaceholderModel)

# ---------------- PAYMENTS / MPESA ----
_maybe_export("pay_mpesa", ["PaymentRequest"], "PaymentRequest", fallback=_PlaceholderModel)
_maybe_export("pay_mpesa", ["PaymentResponse"], "PaymentResponse", fallback=_PlaceholderModel)
_maybe_export("pay_mpesa", ["ConfirmMpesaRequest"], "ConfirmMpesaRequest", fallback=_PlaceholderModel)

# ---------------- BROADCAST -----------
_maybe_export("broadcast", ["BroadcastMessage"], "BroadcastMessage", fallback=_PlaceholderModel)

# ---------------- PLATFORMS -----------
_maybe_export("platforms", ["PlatformConnectRequest"], "PlatformConnectRequest", fallback=_PlaceholderModel)
_maybe_export("platforms", ["PlatformOut"], "PlatformOut", fallback=_PlaceholderModel)

# ---------------- LANGUAGE ------------
_maybe_export("language", ["LanguagePreferenceUpdate"], "LanguagePreferenceUpdate", fallback=_PlaceholderModel)

# ---------------- SCHEDULE ------------
_maybe_export("schedule", ["ScheduledMessageCreate"], "ScheduledMessageCreate", fallback=_PlaceholderModel)
_maybe_export("schedule", ["ScheduledMessageOut"], "ScheduledMessageOut", fallback=_PlaceholderModel)

# ---------------- SETTINGS ------------
_maybe_export("settings", ["SettingsCreate"], "SettingsCreate", fallback=_PlaceholderModel)
_maybe_export("settings", ["SettingsOut"], "SettingsOut", fallback=_PlaceholderModel)

# ---------------- CAMPAIGNS -----------
_maybe_export("campaigns", ["CampaignCreate"], "CampaignCreate", fallback=_PlaceholderModel)
_maybe_export("campaigns", ["CampaignOut"], "CampaignOut", fallback=_PlaceholderModel)

# ---------------- CHAT ----------------
_maybe_export("chat", ["ChatCreate"], "ChatCreate", fallback=_PlaceholderModel)
_maybe_export("chat", ["ChatOut"], "ChatOut", fallback=_PlaceholderModel)

# ---------------- OWNER / ADMIN -------
_maybe_export("owner", ["RoleUpdateRequest"], "RoleUpdateRequest", fallback=_PlaceholderModel)
_maybe_export("owner", ["AdminCreate"], "AdminCreate", fallback=_PlaceholderModel)

# ---------------- SUPPORT (NEW) -------
# Allow: from backend.schemas import SupportTicketOut
_maybe_export("support", ["SupportTicketOut"], "SupportTicketOut", fallback=_PlaceholderModel)

# ---------------- SUBMODULE EXPOSURE ---
# Allow: from backend.schemas import balance_schemas
_expose_module("balance_schemas")

# ---------------- Lazy getattr ----------------
_LAZY_MAP: Dict[str, List[tuple[str, str]]] = {
    "User": [("user", "UserOut"), ("user", "User"), ("user", "UserRead")],
    "UserCreate": [("user", "UserCreate")],
    "UserUpdate": [("user", "UserUpdate")],
    "Token": [("auth", "Token")],
    "TokenData": [("auth", "TokenData")],
    "PostCreate": [("post", "PostCreate")],
    "PostOut": [("post", "PostOut"), ("post", "PostRead")],
    "ForgotPasswordRequest": [("forgot_password", "ForgotPasswordRequest")],
    "VerifyResetCode": [("forgot_password", "VerifyResetCode")],
    "ResetPassword": [("forgot_password", "ResetPassword")],
    "PaymentRequest": [("pay_mpesa", "PaymentRequest")],
    "PaymentResponse": [("pay_mpesa", "PaymentResponse")],
    "ConfirmMpesaRequest": [("pay_mpesa", "ConfirmMpesaRequest")],
    "BroadcastMessage": [("broadcast", "BroadcastMessage")],
    "PlatformConnectRequest": [("platforms", "PlatformConnectRequest")],
    "PlatformOut": [("platforms", "PlatformOut")],
    "LanguagePreferenceUpdate": [("language", "LanguagePreferenceUpdate")],
    "ScheduledMessageCreate": [("schedule", "ScheduledMessageCreate")],
    "ScheduledMessageOut": [("schedule", "ScheduledMessageOut")],
    "SettingsCreate": [("settings", "SettingsCreate")],
    "SettingsOut": [("settings", "SettingsOut")],
    "CampaignCreate": [("campaigns", "CampaignCreate")],
    "CampaignOut": [("campaigns", "CampaignOut")],
    "ChatCreate": [("chat", "ChatCreate")],
    "ChatOut": [("chat", "ChatOut")],
    "RoleUpdateRequest": [("owner", "RoleUpdateRequest")],
    "AdminCreate": [("owner", "AdminCreate")],
    "SupportTicketOut": [("support", "SupportTicketOut")],
}

def __getattr__(name: str) -> Any:
    """Lazy resolver: import on first access; otherwise return a placeholder model."""
    if name in _LAZY_MAP:
        for mod, sym in _LAZY_MAP[name]:
            with suppress(Exception):
                obj = getattr(import_module(f"{__name__}.{mod}"), sym)
                globals()[name] = obj
                return obj
        globals()[name] = _PlaceholderModel
        return _PlaceholderModel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------- Public __all__ ----------------
__all__ = sorted(
    k for k, v in globals().items()
    if k[:1].isupper() and k not in {"BaseModel", "_PlaceholderModel"}
)
