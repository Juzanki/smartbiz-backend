# -*- coding: utf-8 -*-
"""
Mobile-First & Flexible Settings Loader for SmartBiz

- Auto-detects Pydantic v1/v2
- Loads env in priority order:
    1) backend/.env (defaults)
    2) backend/.env.local  (if ENV != production)
       OR backend/.env.production (if ENV=production)
    3) OS environment variables (highest priority)
- Exposes both:
    • settings  (Pydantic Settings instance)
    • Flat constants (DATABASE_URL, SECRET_KEY, PDF_API_KEY, etc.)
- Helpful warnings for missing critical vars
"""

from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import List, Optional

# ---------------- Pydantic v2/v1 detection ----------------
_V2 = True
try:
    # Pydantic v2 with pydantic-settings
    from pydantic_settings import BaseSettings  # type: ignore
except Exception:
    try:
        # Pydantic v2 without pydantic-settings still works for BaseSettings
        from pydantic import BaseSettings  # type: ignore
    except Exception:
        _V2 = False
        try:
            # Pydantic v1 fallback
            from pydantic.env_settings import BaseSettings  # type: ignore
        except Exception as e:
            raise ImportError(
                "Cannot import BaseSettings. Install `pydantic-settings` (v2) "
                "or ensure Pydantic v1/v2 is installed."
            ) from e

# ---------------- dotenv loader (optional) ----------------
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    def load_dotenv(*_, **__):
        # If python-dotenv is not installed, we just skip silently
        pass

# ---------------- Env file discovery & loading ----------------
HERE = Path(__file__).resolve()
BACKEND_DIR = HERE.parent
PROJECT_ROOT = BACKEND_DIR.parent  # e.g., E:/SmartBiz_Assistance

def _load_env_chain() -> str:
    """
    Load env files in order. Returns resolved environment string (lowercased).
    """
    # 0) Base defaults (in backend/.env and optionally at project root)
    for p in (BACKEND_DIR / ".env", PROJECT_ROOT / ".env"):
        load_dotenv(p, override=False)

    # Determine environment (dev/prod) after base defaults
    env_mode = os.getenv("ENVIRONMENT", os.getenv("ENV", "development")).strip().lower()

    if env_mode == "production":
        # 1) Production overrides
        for p in (BACKEND_DIR / ".env.production", PROJECT_ROOT / ".env.production"):
            load_dotenv(p, override=True)
    else:
        # 1) Development overrides
        for p in (BACKEND_DIR / ".env.local", PROJECT_ROOT / ".env.local"):
            load_dotenv(p, override=True)

    # 2) OS env already has highest priority by default
    return env_mode

ENV_MODE = _load_env_chain()

def _split_csv(v: Optional[str]) -> List[str]:
    if not v:
        return []
    return [x.strip() for x in v.split(",") if x.strip()]

def _as_bool(v: Optional[str], default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "t", "yes", "y", "on"}

# ---------------- Settings model ----------------
class Settings(BaseSettings):
    # App
    APP_NAME: str = os.getenv("APP_NAME", "SmartBiz Assistance")
    APP_VERSION: str = os.getenv("APP_VERSION", "1.0.0")
    ENVIRONMENT: str = ENV_MODE
    DEBUG: bool = _as_bool(os.getenv("DEBUG"), default=False)

    # Database
    DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")

    # JWT/Auth
    SECRET_KEY: Optional[str] = os.getenv("SECRET_KEY")
    JWT_ALG: str = os.getenv("JWT_ALG", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
    JWT_ISSUER: Optional[str] = os.getenv("JWT_ISSUER")
    JWT_AUDIENCE: Optional[str] = os.getenv("JWT_AUDIENCE")
    JWT_LEEWAY_SECONDS: int = int(os.getenv("JWT_LEEWAY_SECONDS", "20"))

    # PDF / Invoicing
    PDF_API_KEY: Optional[str] = os.getenv("PDF_API_KEY")
    PDF_SECRET_KEY: Optional[str] = os.getenv("PDF_SECRET_KEY")
    PDF_BASE_URL: str = os.getenv("PDF_BASE_URL", "https://api.examplepdf.com")

    # AI Keys
    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
    OPENAI_BASE_URL: Optional[str] = os.getenv("OPENAI_BASE_URL")
    OPENAI_MODEL: Optional[str] = os.getenv("OPENAI_MODEL")
    PIXELAI_API_KEY: Optional[str] = os.getenv("PIXELAI_API_KEY")
    DEEPSEEK_API_KEY: Optional[str] = os.getenv("DEEPSEEK_API_KEY")

    # CORS / Frontend
    FRONTEND_URL: Optional[str] = os.getenv("FRONTEND_URL")
    CORS_ORIGINS: Optional[str] = os.getenv("CORS_ORIGINS")  # CSV list

    # Pydantic config
    if _V2:
        model_config = {
            "extra": "ignore",
            "env_file": ".env",              # Used when BaseSettings loads .env
            "env_file_encoding": "utf-8",
        }
    else:  # Pydantic v1
        class Config:  # type: ignore
            case_sensitive = False
            env_file = ".env"
            env_file_encoding = "utf-8"

    # Convenience properties (not env fields)
    @property
    def cors_origins_list(self) -> List[str]:
        # Merge FRONTEND_URL and CORS_ORIGINS (CSV) into one list (unique)
        items = set()
        if self.FRONTEND_URL:
            items.add(self.FRONTEND_URL.strip())
        for it in _split_csv(self.CORS_ORIGINS):
            items.add(it)
        return list(items)

# Singleton instance
settings = Settings()

# ---------------- Helpful warnings ----------------
# (We avoid hard-failing so app can still boot for other routes.)
for var in ("DATABASE_URL", "SECRET_KEY"):
    if not getattr(settings, var, None):
        print(
            f"⚠️ WARNING: {var} is not set. Check your .env/.env.local/.env.production.",
            file=sys.stderr,
        )

# If invoice router expects flat constants (legacy code), export them here:
DATABASE_URL: Optional[str] = settings.DATABASE_URL
SECRET_KEY: Optional[str] = settings.SECRET_KEY
ENVIRONMENT: str = settings.ENVIRONMENT
DEBUG: bool = settings.DEBUG

PDF_API_KEY: Optional[str] = settings.PDF_API_KEY
PDF_SECRET_KEY: Optional[str] = settings.PDF_SECRET_KEY
PDF_BASE_URL: str = settings.PDF_BASE_URL

OPENAI_API_KEY: Optional[str] = settings.OPENAI_API_KEY
OPENAI_BASE_URL: Optional[str] = settings.OPENAI_BASE_URL
OPENAI_MODEL: Optional[str] = settings.OPENAI_MODEL
PIXELAI_API_KEY: Optional[str] = settings.PIXELAI_API_KEY
DEEPSEEK_API_KEY: Optional[str] = settings.DEEPSEEK_API_KEY

FRONTEND_URL: Optional[str] = settings.FRONTEND_URL
CORS_ORIGINS: Optional[str] = settings.CORS_ORIGINS
CORS_ORIGINS_LIST: List[str] = settings.cors_origins_list

__all__ = [
    # objects
    "settings",
    # flat constants (legacy-friendly)
    "DATABASE_URL", "SECRET_KEY", "ENVIRONMENT", "DEBUG",
    "PDF_API_KEY", "PDF_SECRET_KEY", "PDF_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
    "PIXELAI_API_KEY", "DEEPSEEK_API_KEY",
    "FRONTEND_URL", "CORS_ORIGINS", "CORS_ORIGINS_LIST",
]
