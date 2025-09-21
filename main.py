# backend/main.py
from __future__ import annotations

import os, sys, re, json, time, uuid, logging, secrets, hashlib, datetime as dt
from pathlib import Path
from contextlib import asynccontextmanager, suppress
from typing import Callable, Iterable, Optional, List, Dict, Any, Tuple

THIS_FILE = Path(__file__).resolve()
BACKEND_DIR = THIS_FILE.parent
ROOT_DIR = BACKEND_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

BACKEND_PUBLIC_URL = os.getenv("BACKEND_PUBLIC_URL","https://smartbiz-backend-p45m.onrender.com").rstrip("/")

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

# ───────────────── DB imports ─────────────────
try:
    from backend.db import Base, SessionLocal, engine
except Exception:
    from db import Base, SessionLocal, engine  # type: ignore

# ───────────────── Env helpers ───────────────
def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    return default if v is None else v.strip().lower() in {"1","true","yes","y","on"}

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

# ───────────────── Logging ───────────────────
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

# ─────────────── User table introspection ────
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
    if _USERS_COLS is not None: return _USERS_COLS
    with suppress(Exception):
        from sqlalchemy import inspect as _sa_inspect
        _USERS_COLS = {c["name"] for c in _sa_inspect(engine).get_columns(USER_TABLE)}
    return _USERS_COLS or set()

# ───────────────── Middlewares ───────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            response: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            return Response(status_code=499)
        except Exception:
            logger.exception("security-mw")
            return JSONResponse(status_code=500, content={"detail":"Internal server error"})
        response.headers.setdefault("X-Content-Type-Options","nosniff")
        response.headers.setdefault("X-Frame-Options","DENY")
        response.headers.setdefault("Referrer-Policy","strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy","geolocation=(), microphone=(), camera=()")
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
            response = JSONResponse(status_code=500, content={"detail":"Internal Server Error"})
        response.headers["x-request-id"] = rid
        response.headers["x-process-time-ms"] = str(int((time.perf_counter() - start) * 1000))
        return response

# ───────────────── DB helpers ────────────────
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

# ───────────────── Lifespan ────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting SmartBiz (env=%s, db=%s)", ENVIRONMENT, _sanitize_db_url(os.getenv("DATABASE_URL","")))
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

# ───────────────── App ─────────────────────
_docs_enabled = env_bool("ENABLE_DOCS", ENVIRONMENT != "production")
app = FastAPI(
    title=os.getenv("APP_NAME","SmartBiz Assistance API"),
    description="SmartBiz Assistance Backend (Render + Netlify)",
    version=os.getenv("APP_VERSION","1.0.0"),
    docs_url=None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

# ───────────────── CORS ────────────────────
def _resolve_cors_origins() -> List[str]:
    hardcoded = [
        "https://smartbizsite.netlify.app",
        "https://smartbiz.site","https://www.smartbiz.site",
        "http://localhost:5173","http://127.0.0.1:5173",
    ]
    return _uniq(hardcoded + env_list("CORS_ORIGINS") + env_list("ALLOWED_ORIGINS"))

ALLOW_ORIGINS = _resolve_cors_origins()
app.add_middleware(CORSMiddleware, allow_origins=ALLOW_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)

# ───────────────── Models ───────────────────
class SignupIn(BaseModel):
    email: EmailStr
    password: constr(min_length=8)
    username: constr(min_length=3)

class LoginIn(BaseModel):
    identifier: str
    password: constr(min_length=1)

# ───────── Password hashing (bcrypt→pbkdf2 fallback) ────────
try:
    from passlib.context import CryptContext  # type: ignore
    _pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    def _hash_password(pw: str) -> str: return _pwd_ctx.hash(pw)
except Exception:
    def _hash_password(pw: str) -> str:
        salt = secrets.token_bytes(16)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
        return f"pbkdf2$sha256$200000${salt.hex()}${dk.hex()}"

# ───────── JWT + Password verify ────────────
JWT_SECRET = os.getenv("JWT_SECRET","change-me-super-secret")
JWT_ISS = os.getenv("JWT_ISS","smartbiz")
JWT_AUD = os.getenv("JWT_AUD","smartbiz-web")
JWT_DAYS = int(os.getenv("JWT_EXPIRE_DAYS","7"))

def _b64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64url_decode(s: str) -> bytes:
    import base64
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)

def _sign_hs256(msg: bytes) -> str:
    import hmac, hashlib
    sig = hmac.new(JWT_SECRET.encode(), msg, hashlib.sha256).digest()
    return _b64url(sig)

def _make_jwt(claims: Dict[str, Any]) -> str:
    header = {"alg":"HS256","typ":"JWT"}
    now = int(time.time())
    payload = {**claims,"iss":JWT_ISS,"aud":JWT_AUD,"iat":now,"nbf":now,"exp":now + JWT_DAYS*86400}
    head = _b64url(json.dumps(header, separators=(",",":")).encode())
    body = _b64url(json.dumps(payload, separators=(",",":")).encode())
    sig  = _sign_hs256(f"{head}.{body}".encode())
    return f"{head}.{body}.{sig}"

def _decode_jwt(token: str) -> Dict[str, Any]:
    try:
        head_b64, body_b64, sig = token.split(".")
    except ValueError:
        raise HTTPException(status_code=401, detail="malformed_token")
    if _sign_hs256(f"{head_b64}.{body_b64}".encode()) != sig:
        raise HTTPException(status_code=401, detail="bad_signature")
    try:
        payload = json.loads(_b64url_decode(body_b64))
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_payload")
    now = int(time.time())
    if payload.get("iss") != JWT_ISS or payload.get("aud") != JWT_AUD:
        raise HTTPException(status_code=401, detail="invalid_claims")
    if now < int(payload.get("nbf",0)) or now > int(payload.get("exp",0)):
        raise HTTPException(status_code=401, detail="token_expired")
    return payload

def _verify_password(plain: str, stored: str) -> bool:
    try:
        if stored.startswith("$2"):  # bcrypt
            return _pwd_ctx.verify(plain, stored)  # type: ignore[name-defined]
    except Exception:
        pass
    if stored.startswith("pbkdf2$sha256$"):
        _, _, iters, salt_hex, dk_hex = stored.split("$", 4)
        dk = hashlib.pbkdf2_hmac("sha256", plain.encode(), bytes.fromhex(salt_hex), int(iters))
        return dk.hex() == dk_hex
    return False

# ───────────── Auth helpers ────────────────
def _get_user_by_identifier(db: Session, identifier: str) -> Optional[Dict[str, Any]]:
    cols = _users_columns()
    where, params = [], {}
    if "email" in cols: where.append("email=:e"); params["e"] = identifier
    if "username" in cols: where.append("username=:u"); params["u"] = identifier
    if not where: return None
    row = db.execute(text(f"SELECT * FROM {USER_TABLE} WHERE {' OR '.join(where)} LIMIT 1"), params).mappings().first()
    return dict(row) if row else None

def _get_user_by_id_or_email(db: Session, uid: Optional[str], email: Optional[str]) -> Optional[Dict[str, Any]]:
    cols = _users_columns()
    if "id" in cols and uid:
        r = db.execute(text(f"SELECT * FROM {USER_TABLE} WHERE id=:i LIMIT 1"), {"i": uid}).mappings().first()
        if r: return dict(r)
    if "email" in cols and email:
        r = db.execute(text(f"SELECT * FROM {USER_TABLE} WHERE email=:e LIMIT 1"), {"e": email}).mappings().first()
        if r: return dict(r)
    return None

def _get_bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer")
    return auth.split(" ",1)[1].strip()

# Dependency: current user
def current_user(request: Request, db: Session = Depends(get_db)) -> Dict[str, Any]:
    token = _get_bearer_token(request)
    payload = _decode_jwt(token)
    user = _get_user_by_id_or_email(db, payload.get("sub") or payload.get("uid"), payload.get("email"))
    if not user: raise HTTPException(status_code=404, detail="user_not_found")
    return {"id": user.get("id"), "email": user.get("email"), "username": user.get("username")}

# ───────────── Routes ──────────────────────
@app.get("/")
def root_redirect(): return RedirectResponse("/health", status_code=302)

@app.get("/health")
def health(): return {"status":"healthy", "db":_db_ping(), "ts": time.time(), "base_url": BACKEND_PUBLIC_URL}

if _docs_enabled:
    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui_html():
        return get_swagger_ui_html(openapi_url=app.openapi_url, title="SmartBiz API Docs")
    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    async def swagger_ui_redirect():
        return get_swagger_ui_oauth2_redirect_html()

# CORS diagnostics (optional)
@app.get("/auth/_diag_cors")
async def cors_diag(request: Request):
    return {
        "origin_header": request.headers.get("origin"),
        "allowed_origins": ALLOW_ORIGINS,
        "auth_header_present": bool(request.headers.get("authorization") or request.headers.get("Authorization")),
    }

# Signup/Register
@app.post("/auth/signup", status_code=status.HTTP_201_CREATED)
@app.post("/auth/register", status_code=status.HTTP_201_CREATED)
def signup(payload: SignupIn, db: Session = Depends(get_db)):
    cols = _users_columns()
    if "email" in cols and _get_user_by_identifier(db, payload.email): raise HTTPException(409, "email_already_exists")
    if "username" in cols and _get_user_by_identifier(db, payload.username): raise HTTPException(409, "username_already_exists")
    pw = _hash_password(payload.password)
    sql = f"INSERT INTO {USER_TABLE} (email, username, {PW_COL}) VALUES (:e,:u,:p) RETURNING id,email,username"
    row = db.execute(text(sql), {"e": payload.email, "u": payload.username, "p": pw}).mappings().first()
    db.commit()
    return {"ok": True, "user": dict(row)}

# Login/Signin
@app.post("/auth/login")
@app.post("/auth/signin")
def login(payload: LoginIn, db: Session = Depends(get_db)):
    row = _get_user_by_identifier(db, payload.identifier)
    if not row: raise HTTPException(404, "user_not_found")
    stored = row.get(PW_COL)
    if not stored or not _verify_password(payload.password, stored):
        raise HTTPException(401, "invalid_credentials")
    token = _make_jwt({"sub": str(row.get("id")), "uid": row.get("id"), "email": row.get("email")})
    return {"ok": True, "access_token": token, "token_type": "bearer",
            "user": {"id": row["id"], "email": row.get("email"), "username": row.get("username")}}

# NEW: /auth/me (decode JWT & return user)
@app.get("/auth/me")
def auth_me(user: Dict[str, Any] = Depends(current_user)):
    return {"ok": True, "user": user}
