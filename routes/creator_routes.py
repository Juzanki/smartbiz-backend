from __future__ import annotations
# backend/routes/creator_routes.py — SmartCreatorAI Feature Generator API
import os
import asyncio
import hashlib
import logging
import uuid
from typing import Optional, List, Dict, Any, Literal
from contextlib import suppress
from datetime import datetime, timezone

from fastapi import (
    APIRouter, Depends, HTTPException, status, Header, Response
)
from pydantic import BaseModel, Field, condecimal, constr
from starlette.concurrency import run_in_threadpool

# -------- Auth (require login). Badilisha kama una require_plan([...]) --------
try:
    from backend.auth import get_current_user
except Exception:
    def get_current_user():
        raise HTTPException(status_code=401, detail="Auth not configured")

# -------- Audit (hiari) --------
def _emit_audit(db, **kw):  # best-effort
    with suppress(Exception):
        from backend.routes.audit_log import emit_audit  # type: ignore
        emit_audit(db, **kw)

# -------- DB (hiari kwa audit/caching) --------
with suppress(Exception):
    from backend.db import get_db  # type: ignore

# -------- Kernel import (with safe fallback) --------
KERNEL_OK = True
try:
    from SmartInjectGPT.creator.smart_creator_kernel import SmartCreatorKernel
except Exception:
    KERNEL_OK = False

    class SmartCreatorKernel:  # fallback stub for dev
        def create_feature_from_prompt(self, prompt: str, **kw) -> Dict[str, Any]:
            # Dev-only mock
            return {
                "feature": {
                    "title": f"Feature (mock): {prompt[:64]}",
                    "spec": {"acceptance_criteria": ["AC1", "AC2"], "estimation": "3d"},
                },
                "progress": {"stage": "generated", "confidence": 0.77},
                "suggestions": ["Refine scope", "Add telemetry events"],
            }

# -------- Logger --------
logger = logging.getLogger("smartbiz.creator")
router = APIRouter(prefix="/creator", tags=["SmartCreator"])

# ====================== Schemas ======================
# Pydantic v1/v2 friendly fields
class FeaturePrompt(BaseModel):
    prompt: constr(min_length=4, max_length=4000)
    language: str = Field("sw", description="Target language code (e.g., sw,en)")
    model: Optional[str] = Field(None, description="Model hint for kernel (optional)")
    temperature: Optional[condecimal(ge=0, le=1)] = None
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)

class FeatureResponse(BaseModel):
    status: Literal["success"]
    request_id: str
    feature: Dict[str, Any]
    progress: Optional[Dict[str, Any]] = None
    suggestions: List[str] = []

class ErrorResponse(BaseModel):
    detail: str

# ====================== Config & Guards ======================
RATE_PER_MIN = int(os.getenv("CREATOR_RATE_PER_MIN", "12"))
REQUEST_TIMEOUT_SEC = int(os.getenv("CREATOR_TIMEOUT_SEC", "60"))
IDEMP_TTL_SEC = int(os.getenv("CREATOR_IDEMP_TTL_SEC", "900"))  # 15m

# in-memory guards (badilisha kwa Redis ikibidi)
_RATE: Dict[int, List[float]] = {}
_IDEMP: Dict[tuple[int, str], float] = {}
_LOCKS: Dict[int, asyncio.Lock] = {}

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _rate_ok(user_id: int) -> None:
    now = asyncio.get_event_loop().time()
    bucket = _RATE.setdefault(user_id, [])
    # safisha >60s
    while bucket and (now - bucket[0]) > 60.0:
        bucket.pop(0)
    if len(bucket) >= RATE_PER_MIN:
        raise HTTPException(status_code=429, detail="Too many requests this minute")
    bucket.append(now)

def _idempotency_check(user_id: int, key: Optional[str]) -> None:
    if not key:
        return
    now = asyncio.get_event_loop().time()
    # cleanup
    stale = [(uid, k) for (uid, k), ts in list(_IDEMP.items()) if (now - ts) > IDEMP_TTL_SEC]
    for s in stale:
        _IDEMP.pop(s, None)
    token = (user_id, key.strip())
    if token in _IDEMP:
        raise HTTPException(status_code=409, detail="Duplicate request (Idempotency-Key)")
    _IDEMP[token] = now

def _etag(payload: Dict[str, Any]) -> str:
    raw = repr(payload).encode("utf-8")
    return 'W/"' + hashlib.sha256(raw).hexdigest()[:16] + '"'

def _get_lock(user_id: int) -> asyncio.Lock:
    lock = _LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[user_id] = lock
    return lock

# ====================== Endpoint ======================
@router.post(
    "/generate-feature",
    response_model=FeatureResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Generate product feature/spec from a natural-language prompt"
)
async def generate_feature(
    data: FeaturePrompt,
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID"),
    current_user=Depends(get_current_user),
    db=Depends(get_db) if "get_db" in globals() else None,
):
    """
    - **Auth required** (uses `get_current_user`)
    - **Idempotent** if `Idempotency-Key` header imetumwa
    - **Rate-limited** per user/dakika
    - **Timeout** (default 60s) na *threadpool* kwa kernel iliyo synchronous
    """
    if not KERNEL_OK:
        logger.warning("SmartCreatorKernel not found; using fallback stub.")

    user_id = getattr(current_user, "id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Guards
    _rate_ok(user_id)
    _idempotency_check(user_id, idempotency_key)

    # Basic normalization
    prompt = data.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    request_id = x_request_id or uuid.uuid4().hex
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Request-ID"] = request_id

    kernel = SmartCreatorKernel()
    lock = _get_lock(user_id)  # serialize per-user kernel calls to be safe

    async with lock:
        try:
            # Run kernel in threadpool & enforce timeout
            def _call():
                return kernel.create_feature_from_prompt(
                    prompt,
                    language=data.language,
                    model=data.model,
                    temperature=float(data.temperature) if data.temperature is not None else None,
                    metadata=data.metadata or {},
                    user_id=user_id,
                    request_id=request_id,
                )

            result = await asyncio.wait_for(run_in_threadpool(_call), timeout=REQUEST_TIMEOUT_SEC)
            if not isinstance(result, dict) or "feature" not in result:
                raise RuntimeError("Kernel returned invalid payload")

            payload = {
                "status": "success",
                "request_id": request_id,
                "feature": result.get("feature", {}),
                "progress": result.get("progress"),
                "suggestions": result.get("suggestions") or [],
            }

            # ETag for client caching/optimistic uses
            response.headers["ETag"] = _etag(payload)

            # Audit (best-effort)
            _emit_audit(
                db,
                action="creator.generate",
                status="success",
                severity="info",
                actor_id=user_id,
                actor_email=getattr(current_user, "email", None),
                resource_type="creator.feature",
                resource_id=request_id,
                meta={"model": data.model, "language": data.language},
            )

            return payload

        except asyncio.TimeoutError as exc:
            logger.exception("Creator timeout (user=%s, req=%s)", user_id, request_id)
            _emit_audit(
                db, action="creator.generate", status="timeout", severity="warn",
                actor_id=user_id, resource_type="creator.feature", resource_id=request_id
            )
            raise HTTPException(status_code=504, detail="Generation timed out") from exc

        except Exception as exc:
            logger.exception("Creator error: %s", exc)
            _emit_audit(
                db, action="creator.generate", status="error", severity="error",
                actor_id=user_id, resource_type="creator.feature", resource_id=request_id,
                meta={"error": str(exc)}
            )
            raise HTTPException(status_code=500, detail="Creator kernel error") from exc
