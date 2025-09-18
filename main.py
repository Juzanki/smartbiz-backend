# backend/main.py
from __future__ import annotations

# ── Path fallback (Render + local) ────────────────────────────────────────────
import os, sys, re, json, time, uuid, base64, hmac, hashlib, logging
from pathlib import Path
from contextlib import asynccontextmanager, suppress
from typing import Callable, Iterable, Optional, List, Tuple, Dict, Any

THIS_FILE = Path(__file__).resolve()
BACKEND_DIR = THIS_FILE.parent
ROOT_DIR = BACKEND_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

# 👉 Backend URL ya Render (tumia yako hapa chini)
BACKEND_PUBLIC_URL = os.getenv(
    "BACKEND_PUBLIC_URL",
    "https://smartbiz-backend-p45m.onrender.com",
).rstrip("/")

import anyio
from fastapi import FastAPI, HTTPException, Request, Depends, status, Form
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
_handler.setFormatter(_JsonFormatter() if LOG_JSON else logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
root_logger = logging.getLogger()
root_logger.handlers = [_handler]
root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("smartbiz.main")

# ────────────────────────── User table introspection (optional) ──────────────
USER_TABLE = os.getenv("USER_TABLE", "users")
PW_COL = os.getenv("SMARTBIZ_PWHASH_COL", "password_hash")
_USERS_COLS: Optional[set[str]] = None

with suppress(Exception):
    from sqlalchemy import inspect as _sa_inspect
    insp = _sa_inspect(engine)
    _USERS_COLS = {c["name"] for c in insp.get_columns(USER_TABLE)}
    chosen = "hashed_password" if "hashed_password" in _USERS_COLS else ("password_hash" if "password_hash" in _USERS_COLS else PW_COL)
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
        # basic hardening
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
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

if _docs_enabled:
    @app.get("/docs", include_in_schema=False)
    def custom_swagger_ui():
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title="SmartBiz API Docs",
            swagger_favicon_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/favicon-32x32.png",
            swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
            swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        )

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    def swagger_ui_redirect():
        return get_swagger_ui_oauth2_redirect_html()

# ────────────────────────── CORS (ikijumuisha backend URL moja kwa moja) ─────
def _resolve_cors_origins() -> List[str]:
    # Hizi ndizo origins halali za frontend zako + backend yako
    hardcoded = [
        "https://smartbizsite.netlify.app",
        "https://smartbiz.site",
        "https://www.smartbiz.site",
        BACKEND_PUBLIC_URL,  # 👈 backend yenyewe (kwa test/tools)
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
    ]
    env_origins = env_list("CORS_ORIGINS") + env_list("ALLOWED_ORIGINS")
    extra = [os.getenv(k) for k in ("FRONTEND_URL", "WEB_URL", "NETLIFY_PUBLIC_URL", "RENDER_PUBLIC_URL")]
    return _uniq([*env_origins, *[x for x in extra if x], *hardcoded])

ALLOW_ORIGINS = _resolve_cors_origins()
CORS_ALLOW_ALL = env_bool("CORS_ALLOW_ALL", False)

if CORS_ALLOW_ALL:
    app.add_middleware(CORSMiddleware, allow_origin_regex=".*", allow_credentials=True, allow_methods=["*"], allow_headers=["*"], expose_headers=["set-cookie"], max_age=600)
    logger.warning("CORS_ALLOW_ALL=1 (dev mode) — avoid in production")
else:
    app.add_middleware(CORSMiddleware, allow_origins=ALLOW_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"], expose_headers=["set-cookie"], max_age=600)
    logger.info("CORS allow_origins: %s", ALLOW_ORIGINS)

class EnsureCorsCredentialsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response: Response = await call_next(request)
        origin = request.headers.get("origin")
        if origin and (CORS_ALLOW_ALL or origin in ALLOW_ORIGINS):
            response.headers.setdefault("Access-Control-Allow-Credentials", "true")
        return response

app.add_middleware(EnsureCorsCredentialsMiddleware)

# Preflight kwa routes zote
@app.options("/{rest_of_path:path}", include_in_schema=False)
async def any_options(_: Request, rest_of_path: str):  # noqa: ARG001
    return Response(status_code=204)

# Proxy headers (Render) & Trusted hosts
with suppress(Exception):
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

trusted_hosts = _uniq(env_list("TRUSTED_HOSTS") + ["*", ".onrender.com", ".smartbiz.site", "localhost", "127.0.0.1", BACKEND_PUBLIC_URL.replace("https://","")])
app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

# Compression + security + request-id + cookie policy
app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(CookiePolicyMiddleware)

# ────────────────────────── Static ────────────────────────────────────────────
class _Uploads(StaticFiles):
    def is_not_modified(self, scope, request_headers, stat_result, etag=None):  # noqa: ANN001
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

# ────────────────────────── Tiny Auth Fallback ────────────────────────────────
SECRET = (os.getenv("AUTH_SECRET") or os.getenv("SECRET_KEY") or "dev-secret").encode("utf-8")
ACCESS_TTL = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "3600"))
ACCESS_COOKIE = os.getenv("ACCESS_COOKIE", "sb_access")

def _sign(payload: str) -> str:
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")

def _mint_token(user_id: str, ttl: int = ACCESS_TTL) -> str:
    now = int(time.time())
    raw = f"{user_id}.{now}.{ttl}.{uuid.uuid4().hex}"
    return raw + "." + _sign(raw)

def _verify_token(token: str) -> Tuple[bool, Optional[str]]:
    try:
        p, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(_sign(p), sig):
            return False, None
        user_id, ts, ttl, _ = p.split(".", 3)
        if int(time.time()) > int(ts) + int(ttl):
            return False, None
        return True, user_id
    except Exception:
        return False, None

def _set_access_cookie(resp: Response, token: str, ttl: int = ACCESS_TTL) -> None:
    resp.set_cookie(key=ACCESS_COOKIE, value=token, max_age=ttl, secure=True, httponly=True, samesite="none", path="/")

def _clear_access_cookie(resp: Response) -> None:
    resp.delete_cookie(key=ACCESS_COOKIE, path="/")

# Password hashing (bcrypt preferred; PBKDF2 fallback)
def _hash_password(pw: str) -> str:
    try:
        from passlib.hash import bcrypt  # type: ignore
        return bcrypt.using(rounds=12).hash(pw)
    except Exception:
        salt = os.urandom(16)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 120_000)
        return "pbkdf2$" + base64.b64encode(salt + dk).decode()

def _verify_password(pw: str, stored: str) -> bool:
    if stored.startswith("$2b$") or stored.startswith("$2a$"):
        try:
            from passlib.hash import bcrypt  # type: ignore
            return bcrypt.verify(pw, stored)
        except Exception:
            return False
    if stored.startswith("pbkdf2$"):
        try:
            blob = base64.b64decode(stored.split("$", 1)[1].encode())
            salt, dk = blob[:16], blob[16:]
            cand = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 120_000)
            return hmac.compare_digest(dk, cand)
        except Exception:
            return False
    return stored == pw

# Simple SQL helpers
def _get_user_by_email(db: Session, email: str) -> Optional[dict]:
    row = db.execute(text(f'SELECT * FROM {USER_TABLE} WHERE LOWER(email)=LOWER(:e) LIMIT 1'), {"e": email}).mappings().first()
    return dict(row) if row else None

def _get_user_by_username(db: Session, uname: str) -> Optional[dict]:
    cols = _users_columns()
    for cand in ("username", "user_name", "handle"):
        if cand in cols:
            row = db.execute(text(f'SELECT * FROM {USER_TABLE} WHERE LOWER("{cand}")=LOWER(:u) LIMIT 1'), {"u": uname}).mappings().first()
            if row:
                return dict(row)
    return None

def _get_user_by_id(db: Session, user_id: str) -> Optional[dict]:
    row = db.execute(text(f'SELECT * FROM {USER_TABLE} WHERE id::text=:i LIMIT 1'), {"i": user_id}).mappings().first()
    return dict(row) if row else None

def _create_user(db: Session, email: str, username: str, password: str) -> dict:
    hpw = _hash_password(password)
    cols = _users_columns()
    uname_col = next((c for c in ("username","user_name","handle") if c in cols), "username")
    sql = text(f"""
        INSERT INTO {USER_TABLE} (email, "{uname_col}", {PW_COL})
        VALUES (:email, :username, :hpw)
        RETURNING id, email, "{uname_col}" AS username
    """)
    try:
        row = db.execute(sql, {"email": email, "username": username, "hpw": hpw}).mappings().first()
        db.commit()
        return dict(row) if row else {"email": email, "username": username}
    except Exception as e:
        db.rollback()
        msg = str(e)
        if any(k in msg.lower() for k in ("unique", "duplicate", "23505")):
            raise HTTPException(status_code=409, detail="Email already registered")
        logger.exception("create_user")
        raise HTTPException(status_code=500, detail="Failed to create user")

# Pydantic models
class SignupIn(BaseModel):
    email: EmailStr
    password: constr(min_length=6)
    username: constr(min_length=2)

class LoginIn(BaseModel):
    identifier: str
    password: constr(min_length=1)

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: Optional[Dict[str, Any]] = None

# ────────────────────────── Health & Diag + Meta ──────────────────────────────
@app.get("/")
def root_redirect():
    # kurahisisha test; peleka docs kama zimewezeshwa, la sivyo health
    return RedirectResponse("/docs" if _docs_enabled else "/api/health", status_code=302)

@app.get("/api/meta/baseurl")
def meta_baseurl():
    return {"base_url": BACKEND_PUBLIC_URL}

@app.get("/health")
@app.get("/api/health")
@app.get("/api/healthz")
def health():
    return {"status": "healthy", "database": _db_ping(), "ts": time.time(), "base_url": BACKEND_PUBLIC_URL}

@app.get("/ready")
@app.get("/api/ready")
def ready():
    return {"status": "ready", "database": _db_ping()}

@app.get("/version")
def version():
    return {"name": os.getenv("APP_NAME", "SmartBiz Assistance API"),
            "version": os.getenv("APP_VERSION", "1.0.0"),
            "env": ENVIRONMENT}

@app.api_route("/api/echo", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"])
async def echo(request: Request):
    body = None
    with suppress(Exception):
        raw = await request.body()
        if raw and len(raw) > 1024 * 1024:
            body = "<omitted: too large>"
        else:
            body = raw.decode(errors="ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
    return {"method": request.method, "url": str(request.url), "headers": dict(request.headers), "cookies": request.cookies, "body": body}

@app.get("/api/_diag/headers")
def diag_headers(request: Request):
    return {"headers": dict(request.headers)}

@app.get("/api/_diag/cors")
def diag_cors():
    return {"allow_origins": ALLOW_ORIGINS, "allow_all": CORS_ALLOW_ALL, "base": BACKEND_PUBLIC_URL}

# ────────────────────────── Prefer real auth router, else fallback ───────────
def _include_auth_routers():
    try:
        from backend.routes.auth_routes import router as auth_router, legacy_router  # type: ignore
        app.include_router(auth_router, prefix="/api")
        app.include_router(legacy_router, prefix="/api/auth")
        logger.info("Loaded auth routers from backend.routes.auth_routes")
        return
    except Exception:
        logger.warning("Auth routers not found; using fallback endpoints")

    # Fallback auth
    def _verify_login_identifier(db: Session, identifier: str, password: str) -> Optional[dict]:
        u = _get_user_by_email(db, identifier) if "@" in identifier else None
        if not u:
            u = _get_user_by_username(db, identifier) or _get_user_by_email(db, identifier)
        if not u:
            return None
        stored = u.get(PW_COL) or u.get("hashed_password") or u.get("password") or ""
        return ({"id": str(u.get("id")), "email": u.get("email"),
                 "username": u.get("username") or u.get("user_name") or u.get("handle")}
                if stored and _verify_password(password, stored) else None)

    @app.post("/api/auth/register", response_model=TokenOut)
    def register(payload: SignupIn, db: Session = Depends(get_db)):
        if _get_user_by_email(db, payload.email):
            raise HTTPException(status_code=409, detail="Email already registered")
        user = _create_user(db, payload.email, payload.username, payload.password)
        access = _mint_token(user_id=str(user.get("id") or user["email"]), ttl=ACCESS_TTL)
        resp = JSONResponse({"access_token": access, "token_type": "bearer", "user": user},
                            status_code=status.HTTP_201_CREATED)
        _set_access_cookie(resp, access, ACCESS_TTL)
        return resp

    @app.post("/api/auth/signup", response_model=TokenOut)
    def signup_alias(payload: SignupIn, db: Session = Depends(get_db)):
        return register(payload, db)  # type: ignore

    @app.post("/api/auth/login", response_model=TokenOut)
    def login(payload: LoginIn, db: Session = Depends(get_db)):
        user = _verify_login_identifier(db, payload.identifier, payload.password)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        access = _mint_token(user_id=str(user.get("id") or user["email"]), ttl=ACCESS_TTL)
        resp = JSONResponse({"access_token": access, "token_type": "bearer", "user": user})
        _set_access_cookie(resp, access, ACCESS_TTL)
        return resp

    @app.post("/api/auth/login-form", response_model=TokenOut)
    def login_form(username: Optional[str] = Form(None), email: Optional[str] = Form(None),
                   password: str = Form(...), db: Session = Depends(get_db)):
        ident = (email or username or "").strip()
        if not ident:
            raise HTTPException(status_code=422, detail="identifier required")
        return login(LoginIn(identifier=ident, password=password), db)  # type: ignore

    @app.post("/api/auth/token/refresh", response_model=TokenOut)
    def refresh_token(request: Request, db: Session = Depends(get_db)):
        raw = request.cookies.get(ACCESS_COOKIE) or ""
        if not raw:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                raw = auth.split(" ", 1)[1].strip()
        if not raw:
            raise HTTPException(status_code=401, detail="Missing token")
        ok, user_id = _verify_token(raw)
        if not ok or not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        user = _get_user_by_id(db, user_id) or {"id": user_id}
        access = _mint_token(user_id=user_id, ttl=ACCESS_TTL)
        resp = JSONResponse({"access_token": access, "token_type": "bearer", "user": user})
        _set_access_cookie(resp, access, ACCESS_TTL)
        return resp

    @app.post("/api/auth/logout", status_code=204)
    def logout():
        resp = Response(status_code=204)
        _clear_access_cookie(resp)
        return resp

    @app.get("/api/auth/me")
    def me(request: Request, db: Session = Depends(get_db)):
        raw = request.cookies.get(ACCESS_COOKIE) or ""
        if not raw:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                raw = auth.split(" ", 1)[1].strip()
        if not raw:
            raise HTTPException(status_code=401, detail="Missing token")
        ok, user_id = _verify_token(raw)
        if not ok or not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        u = _get_user_by_id(db, user_id)
        return {"id": user_id, "email": (u or {}).get("email"), "username": (u or {}).get("username")}

    @app.get("/api/auth/session/verify")
    def verify_session(request: Request):
        raw = request.cookies.get(ACCESS_COOKIE) or ""
        if not raw:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                raw = auth.split(" ", 1)[1].strip()
        ok, user_id = _verify_token(raw) if raw else (False, None)
        return {"valid": bool(ok), "user": {"id": user_id} if ok else None}

_include_auth_routers()

# ────────────────────────── Local runner ──────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app",
                host="0.0.0.0",
                port=int(os.getenv("PORT", "8000")),
                reload=env_bool("DEV_RELOAD", True))
