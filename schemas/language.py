# backend/schemas/language.py
from __future__ import annotations

from typing import Optional

# --- Pydantic v2 kwanza, v1 fallback (shim fupi & nyepesi) -------------------
_V2 = True
try:
    from pydantic import BaseModel, Field, ConfigDict, field_validator
except Exception:  # Pydantic v1
    _V2 = False
    from pydantic import BaseModel, Field  # type: ignore
    from pydantic import validator as _v  # type: ignore
    ConfigDict = dict  # type: ignore

    def field_validator(field_name: str, *, mode: str = "after"):  # type: ignore
        pre = (mode == "before")
        def deco(fn):
            return _v(field_name, pre=pre, allow_reuse=True)(fn)  # type: ignore
        return deco


# ------------------------------ Base (forbid extras) ------------------------
class _Base(BaseModel):
    if _V2:
        model_config = ConfigDict(extra="forbid")
    else:
        class Config:  # type: ignore
            extra = "forbid"


# ------------------------------ Schemas -------------------------------------
class LanguagePreferenceUpdate(_Base):
    # Mfano: "en", "sw", "fr", "en-US"
    language: str = Field(..., min_length=2, max_length=15, description="IETF tag")
    # Ruhusu majina mepesi ya sauti (alfanumeriki + _.-)
    voice: Optional[str] = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            json_schema_extra={"example": {"language": "sw", "voice": "female_01"}},
        )
    else:
        class Config(_Base.Config):  # type: ignore
            schema_extra = {"example": {"language": "sw", "voice": "female_01"}}

    # ------------------------- Validators nyepesi ----------------------------
    @field_validator("language", mode="before")
    def _norm_lang(cls, v):
        s = str(v or "").strip()
        if not s:
            raise ValueError("language is required")
        s = s.replace("_", "-")
        parts = s.split("-")

        # primary subtag: 2–3 herufi, lazima iwepo
        if not parts or not parts[0].isalpha() or not (2 <= len(parts[0]) <= 3):
            raise ValueError("invalid language code")

        # normalize: primary lower, region (ikiwa 2/3 letters) upper
        parts[0] = parts[0].lower()
        if len(parts) >= 2 and parts[1].isalpha() and len(parts[1]) in (2, 3):
            parts[1] = parts[1].upper()

        s = "-".join(p for p in parts if p)
        if len(s) > 15:
            raise ValueError("language code too long")
        return s

    @field_validator("voice", mode="before")
    def _norm_voice(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None
