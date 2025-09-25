# backend/routes/auth.py
"""
Shim ya kuelekeza 'backend.routes.auth' kwenda 'backend.routes.auth_routes'.
Inaruhusu app.yetu kuitafuta 'auth' (si 'auth_routes') na kupata `router`.
"""

try:
    # Mpangilio wa kawaida: faili auth_routes.py iko kwenye pakiti hii hii
    from .auth_routes import router  # type: ignore
except Exception as e:
    # Njia ya pili iwapo importer anatumia jina kamili la pakiti
    try:
        from backend.routes.auth_routes import router  # type: ignore
    except Exception as e2:
        raise ImportError(
            "Cannot import 'router' from auth_routes. "
            "Hakikisha 'backend/routes/auth_routes.py' ipo na ina `router = APIRouter(...)`."
        ) from e2

__all__ = ["router"]
