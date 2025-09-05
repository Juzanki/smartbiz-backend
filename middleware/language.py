from backend.schemas.user import UserOut
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request
from pathlib import Path
import gettext

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
DOMAIN = "messages"

class LanguageMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        lang_header = request.headers.get("Accept-Language", "en")
        lang_code = lang_header.split(",")[0].split(";")[0].strip()

        try:
            translation = gettext.translation(DOMAIN, localedir=LOCALES_DIR, languages=[lang_code])
        except FileNotFoundError:
            translation = gettext.translation(DOMAIN, localedir=LOCALES_DIR, languages=["en"], fallback=True)

        translation.install()
        response = await call_next(request)
        return response

language_middleware = LanguageMiddleware

