# backend/main.py
from __future__ import annotations

# ── Path fallback (Render + local) ────────────────────────────────────────────
import os, sys, re, json, time, uuid, logging, secrets, hashlib, datetime as dt
from pathlib import Path
from contextlib import asynccontextmanager, suppress
from typing import Callable, Iterable, Optional, List, Dict, Any

THIS_FILE = Path(__file__).resolve()
BACKEND_DIR = THIS_FILE.parent
ROOT_DIR = BACKEND_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

BACKEND_PUBLIC_URL = os.getenv(
    "BACKEND_PUBLIC_URL",
    "https://smartbiz-backend-p45m.onrender.com",
).rstrip("/")

import anyio
from fastapi import FastAPI, HTTPException, Request, Depends, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, constr
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, RedirectResponse
from starlette.datastructures import MutableHeaders

# ── DB imports (package-first, then relative) ─────────────────────────────────
try:
    from backend.db import Base, SessionLocal, engine
except Exception:  # pragma: no cover
    from db import Base, SessionLocal, engine  # type: ignore

# ────────────────────────── Env helpers ───────────────────────────────────────
def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "y", "on"}

def env_list(key: str) -> List[str]:
    raw = os.getenv(key, "")
    return [x.strip() for x in raw.split(",") if x and x.strip()]

def _uniq(items: Iterable[Optional[str]]) -> List[str]:
    out, seen = [], set()
    for x in items:
        if x and x not in seen:
            out.append(x); seen.add(x)
    return out

def _sanitize_db_url(url: str) -> str:
    return "" if not url else re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:****@", url)

ENVIRONMENT = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "production").lower()
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", BACKEND_DIR / "uploads")).resolve()
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# ────────────────────────── Logging ───────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_JSON = env_bool("LOG_JSON", False)

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "lvl": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

_handler = logging.StreamHandler()
_handler.setFormatter(
    _JsonFormatter() if LOG_JSON else logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
)
root_logger = logging.getLogger()
root_logger.handlers = [_handler]
root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("smartbiz.main")

# ────────────────────────── User table introspection ──────────────────────────
USER_TABLE = os.getenv("USER_TABLE", "users")
PW_COL = os.getenv("SMARTBIZ_PWHASH_COL", "password_hash")
_USERS_COLS: Optional[set[str]] = None

with suppress(Exception):
    from sqlalchemy import inspect as _sa_inspect
    insp = _sa_inspect(engine)
    _USERS_COLS = {c["name"] for c in insp.get_columns(USER_TABLE)}
    chosen = "hashed_password" if "hashed_password" in _USERS_COLS else (
        "password_hash" if "password_hash" in _USERS_COLS else PW_COL
    )
    os.environ.setdefault("SMARTBIZ_PWHASH_COL", chosen)
    PW_COL = chosen
    logger.info("Users cols: %s | PW col: %s", sorted(_USERS_COLS or []), PW_COL)

def _users_columns() -> set[str]:
    global _USERS_COLS
    if _USERS_COLS is not None:
        return _USERS_COLS
    with suppress(Exception):
        from sqlalchemy import inspect as _sa_inspect
        _USERS_COLS = {c["name"] for c in _sa_inspect(engine).get_columns(USER_TABLE)}
    return _USERS_COLS or set()

# ────────────────────────── Middlewares ───────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            response: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            return Response(status_code=499)
        except Exception:
            logger.exception("security-mw")
            return JSONResponse(status_code=500, content={"detail": "Internal server error"})
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https:; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
        )
        return response

class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            response = Response(status_code=499)
        except Exception:
            logger.exception("reqid-mw")
            response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
        response.headers["x-request-id"] = rid
        response.headers["x-process-time-ms"] = str(int((time.perf_counter() - start) * 1000))
        return response

FORCE_SAMESITE_NONE = env_bool("COOKIE_SAMESITE_NONE", True)
FORCE_HTTPONLY = env_bool("COOKIE_HTTPONLY_DEFAULT", True)

class CookiePolicyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response: Response = await call_next(request)
        if not (FORCE_SAMESITE_NONE or FORCE_HTTPONLY):
            return response
        raw = list(response.raw_headers)
        cookie_values: List[str] = []
        mutated = False
        for name, value in raw:
            if name.lower() == b"set-cookie":
                s = value.decode("latin-1")
                if FORCE_SAMESITE_NONE:
                    s = re.sub(r"(?i)SameSite=\w+", "SameSite=None", s) if "samesite=" in s.lower() else s + "; SameSite=None"
                    if "secure" not in s.lower():
                        s += "; Secure"
                if FORCE_HTTPONLY and "httponly" not in s.lower():
                    s += "; HttpOnly"
                cookie_values.append(s); mutated = True
        if mutated:
            headers = MutableHeaders(response.headers)
            headers.pop("set-cookie", None)
            for cv in cookie_values:
                headers.append("set-cookie", cv)
        return response

# ────────────────────────── DB helpers ────────────────────────────────────────
def _db_ping() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ────────────────────────── Lifespan ──────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting SmartBiz (env=%s, db=%s)", ENVIRONMENT, _sanitize_db_url(os.getenv("DATABASE_URL", "")))
    if not _db_ping():
        logger.error("Database connection failed at startup")
    else:
        logger.info("Database OK")
    if env_bool("AUTO_CREATE_TABLES", ENVIRONMENT != "production"):
        with suppress(Exception):
            Base.metadata.create_all(bind=engine, checkfirst=True)
            logger.info("Tables verified/created")
    try:
        yield
    finally:
        logger.info("Shutting down SmartBiz")

# ────────────────────────── App ───────────────────────────────────────────────
_docs_enabled = env_bool("ENABLE_DOCS", ENVIRONMENT != "production")
app = FastAPI(
    title=os.getenv("APP_NAME", "SmartBiz Assistance API"),
    description="SmartBiz Assistance Backend (Render + Netlify)",
    version=os.getenv("APP_VERSION", "1.0.0"),
    docs_url=None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

# ────────────────────────── CORS (robust + env-driven) ────────────────────────
def _resolve_cors_origins() -> List[str]:
    hardcoded = [
        "https://smartbizsite.netlify.app",
        "https://smartbiz.site",
        "https://www.smartbiz.site",
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
    ]
    env_origins = env_list("CORS_ORIGINS") + env_list("ALLOWED_ORIGINS")
    extra = [os.getenv(k) for k in ("FRONTEND_URL", "WEB_URL", "NETLIFY_PUBLIC_URL")]
    return _uniq([*env_origins, *[x for x in extra if x], *hardcoded])

ALLOW_ORIGINS = _resolve_cors_origins()
CORS_ALLOW_ALL = env_bool("CORS_ALLOW_ALL", False)

if CORS_ALLOW_ALL:
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["set-cookie"],
        max_age=600,
    )
    logger.warning("CORS_ALLOW_ALL=1 (dev mode) — avoid in production")
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOW_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["set-cookie"],
        max_age=600,
    )
    logger.info("CORS allow_origins: %s", ALLOW_ORIGINS)

# Optional compression + basic security/ids
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(CookiePolicyMiddleware)
# Trusted hosts if you use custom domains (optional)
if env_bool("ENABLE_TRUSTED_HOSTS", False):
    hosts = env_list("TRUSTED_HOSTS") or ["smartbiz.site", "smartbizsite.netlify.app", "localhost"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

# ────────────────────────── Static ────────────────────────────────────────────
class _Uploads(StaticFiles):
    def is_not_modified(self, scope, request_headers, stat_result, etag=None):
        return super().is_not_modified(scope, request_headers, stat_result, etag)

app.mount("/uploads", _Uploads(directory=str(UPLOADS_DIR)), name="uploads")

# ────────────────────────── Exceptions ────────────────────────────────────────
@app.exception_handler(ClientDisconnect)
async def client_disconnect_handler(_: Request, __: ClientDisconnect):
    return Response(status_code=499)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    payload = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
    return JSONResponse(status_code=exc.status_code, content=payload)

@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception):
    if isinstance(exc, anyio.EndOfStream):
        return Response(status_code=499)
    logger.exception("unhandled")
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

# ────────────────────────── Pydantic Models ───────────────────────────────────
class SignupIn(BaseModel):
    email: EmailStr
    password: constr(min_length=8)
    username: constr(min_length=3, pattern=r"^[a-z0-9_]+$")

class LoginIn(BaseModel):
    identifier: str
    password: constr(min_length=1)

class UserOut(BaseModel):
    id: Any
    email: EmailStr
    username: str
    created_at: Optional[str] = None

# ────────────────────────── Password hashing (bcrypt→fallback) ────────────────
try:
    # Recommended path
    from passlib.context import CryptContext  # type: ignore
    _pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    def _hash_password(pw: str) -> str:
        return _pwd_ctx.hash(pw)
except Exception:
    # Fallback: PBKDF2-HMAC-SHA256 (salted) — still secure
    def _hash_password(pw: str) -> str:
        salt = secrets.token_bytes(16)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000)
        return "pbkdf2$sha256$200000$" + salt.hex() + "$" + dk.hex()

# ────────────────────────── Health & Docs ─────────────────────────────────────
@app.get("/")
def root_redirect():
    return RedirectResponse("/health", status_code=302)

@app.get("/health")
def health():
    return {"status": "healthy", "database": _db_ping(), "ts": time.time(), "base_url": BACKEND_PUBLIC_URL}

if _docs_enabled:
    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui_html():
        return get_swagger_ui_html(openapi_url=app.openapi_url, title="SmartBiz API Docs")

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    async def swagger_ui_redirect():
        return get_swagger_ui_oauth2_redirect_html()

# ────────────────────────── CORS DIAG ─────────────────────────────────────────
@app.get("/auth/_diag_cors")
async def cors_diag(request: Request):
    return {
        "method": request.method,
        "origin_header": request.headers.get("origin"),
        "access_control_request_method": request.headers.get("access-control-request-method"),
        "access_control_request_headers": request.headers.get("access-control-request-headers"),
        "allowed_origins": ALLOW_ORIGINS if not CORS_ALLOW_ALL else ["* (regex)"],
        "credentials_required": True,
    }

# ────────────────────────── SIGNUP / REGISTER (shared handler) ────────────────
# Tunajaribu kutumia ORM 'User' kama ipo; vinginevyo tunatumia SQL ya moja kwa moja.
try:
    from backend.models.user import User  # type: ignore
except Exception:
    try:
        from models.user import User  # type: ignore
    except Exception:
        User = None  # type: ignore

def _now_iso() -> str:
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).isoformat()

def _select_one(db: Session, query: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    row = db.execute(text(query), params).mappings().first()
    return dict(row) if row else None

def _create_user_sql(db: Session, payload: SignupIn) -> Dict[str, Any]:
    cols = _users_columns()
    if not cols:
        raise HTTPException(status_code=500, detail="Users table not introspectable.")

    # 1) duplicates
    if "email" in cols:
        existing = _select_one(db, f"SELECT id, email FROM {USER_TABLE} WHERE email=:e LIMIT 1", {"e": payload.email})
        if existing:
            raise HTTPException(status_code=409, detail="email_already_exists")
    if "username" in cols:
        existing = _select_one(db, f"SELECT id, username FROM {USER_TABLE} WHERE username=:u LIMIT 1", {"u": payload.username})
        if existing:
            raise HTTPException(status_code=409, detail="username_already_exists")

    # 2) insert minimal set dynamically
    insert_cols: List[str] = []
    params: Dict[str, Any] = {}

    if "email" in cols:
        insert_cols.append("email"); params["email"] = str(payload.email)
    if "username" in cols:
        insert_cols.append("username"); params["username"] = payload.username
    if PW_COL in cols:
        insert_cols.append(PW_COL); params[PW_COL] = _hash_password(payload.password)
    # nice-to-have fields if exist
    now = _now_iso()
    for c in ("created_at", "updated_at", "created_on", "joined_at"):
        if c in cols:
            insert_cols.append(c); params[c] = now
    for c, v in (("is_active", True), ("is_verified", False), ("status", "active")):
        if c in cols:
            insert_cols.append(c); params[c] = v

    if not insert_cols:
        raise HTTPException(status_code=500, detail="No compatible columns to insert.")

    placeholders = ",".join([f":{c}" for c in insert_cols])
    sql = f"INSERT INTO {USER_TABLE} ({','.join(insert_cols)}) VALUES ({placeholders}) RETURNING id, email, username"
    row = db.execute(text(sql), params).mappings().first()
    db.commit()
    return {"id": row["id"], "email": row.get("email"), "username": row.get("username")}

def _create_user_orm(db: Session, payload: SignupIn) -> Dict[str, Any]:
    # Safely set attributes that exist
    # duplicates
    if hasattr(User, "email"):
        q = db.query(User).filter(getattr(User, "email") == str(payload.email)).first()
        if q: raise HTTPException(status_code=409, detail="email_already_exists")
    if hasattr(User, "username"):
        q = db.query(User).filter(getattr(User, "username") == payload.username).first()
        if q: raise HTTPException(status_code=409, detail="username_already_exists")

    user = User()  # type: ignore
    if hasattr(user, "email"): setattr(user, "email", str(payload.email))
    if hasattr(user, "username"): setattr(user, "username", payload.username)
    # password column resolved earlier
    setattr(user, PW_COL, _hash_password(payload.password))
    # optional flags
    for c, v in (("is_active", True), ("is_verified", False), ("status", "active")):
        if hasattr(user, c): setattr(user, c, v)
    now = dt.datetime.utcnow()
    for c in ("created_at", "updated_at", "created_on", "joined_at"):
        if hasattr(user, c): setattr(user, c, now)

    db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "id": getattr(user, "id", None),
        "email": getattr(user, "email", None),
        "username": getattr(user, "username", None),
        "created_at": str(getattr(user, "created_at", None)) if hasattr(user, "created_at") else None,
    }

def _signup_core(db: Session, payload: SignupIn) -> Dict[str, Any]:
    if User is not None:
        try:
            return _create_user_orm(db, payload)
        except Exception as e:
            logger.warning("ORM create failed, will fallback to SQL: %s", e)
    return _create_user_sql(db, payload)

@app.post("/auth/signup", status_code=status.HTTP_201_CREATED)
@app.post("/auth/register", status_code=status.HTTP_201_CREATED)  # alias
def signup(payload: SignupIn, db: Session = Depends(get_db)):
    user = _signup_core(db, payload)
    return {"ok": True, "user": user}

# ────────────────────────── (Optionally keep a tiny echo tester) ──────────────
@app.post("/auth/_echo")  # kwa debugging ya CORS tu
async def register_echo(payload: dict):
    logger.info("register_echo payload keys: %s", list(payload.keys()))
    return {"ok": True, "data": payload, "note": "Echo OK"}

# ────────────────────────── DONE: routers zako zingine unaweza kuongeza hapa ─
# Mfano:
# from backend.routes.auth_routes import router as auth_router
# app.include_router(auth_router, prefix="/auth")
