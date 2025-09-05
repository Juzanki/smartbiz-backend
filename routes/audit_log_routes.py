from __future__ import annotations
# backend/routes/audit_log.py
import os
import csv
import json
import hashlib
from io import StringIO
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Iterable, AsyncGenerator

from fastapi import (
    APIRouter, Depends, HTTPException, Query, Path, Response, status, Request, Header
)
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import JSON as SA_JSON
try:
    from sqlalchemy.dialects.postgresql import JSONB  # type: ignore
    JSON_VARIANT = SA_JSON().with_variant(JSONB, "postgresql")
except Exception:  # pragma: no cover
    JSON_VARIANT = SA_JSON()
from sqlalchemy import func, or_, and_
from sqlalchemy import JSON as SA_JSON
try:
    from sqlalchemy.dialects.postgresql import JSONB  # type: ignore
    JSON_VARIANT = SA_JSON().with_variant(JSONB, "postgresql")
except Exception:  # pragma: no cover
    JSON_VARIANT = SA_JSON()

from backend.db import get_db

# --------- Model import (support both layouts) ----------
try:
    from backend.models.audit_log import AuditLog as AuditLogModel
except Exception:  # pragma: no cover
    from backend.models.audit_log import AuditLog as AuditLogModel

# --------- Schemas (use yours, fallback if missing) ----------
try:
    from backend.schemas.audit_log import AuditLogOut, AuditLogCreate, AuditLogStats
except Exception:  # pragma: no cover
    from pydantic import BaseModel, Field

    class AuditLogCreate(BaseModel):
        action: str = Field(..., min_length=2, max_length=100)
        resource_type: Optional[str] = Field(None, max_length=100)
        resource_id: Optional[str] = Field(None, max_length=100)
        status: str = Field("success", max_length=20)  # success|failed|info|warn
        severity: str = Field("info", max_length=20)   # info|low|medium|high|critical
        actor_id: Optional[int] = None
        actor_email: Optional[str] = None
        ip: Optional[str] = None
        user_agent: Optional[str] = None
        meta: Optional[Dict[str, Any]] = None

    class AuditLogOut(AuditLogCreate):
        id: int
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None

    class AuditLogStats(BaseModel):
        by_action: Dict[str, int] = {}
        by_status: Dict[str, int] = {}
        by_severity: Dict[str, int] = {}

# --------- RBAC guard (admin/owner) ----------
try:
    from backend.dependencies import check_admin as _check_admin  # type: ignore
    def admin_guard(_: Any = Depends(_check_admin)) -> None:
        return None
except Exception:
    try:
        from backend.dependencies import get_current_user  # type: ignore
        def admin_guard(user: Any = Depends(get_current_user)) -> None:
            if getattr(user, "role", None) not in {"admin", "owner"}:
                raise HTTPException(status_code=403, detail="Not authorized")
    except Exception:
        def admin_guard() -> None:
            raise HTTPException(status_code=403, detail="Admin guard missing")

router = APIRouter(prefix="/audit", tags=["Audit Logs"])

# --------- Config / constants ----------
ALLOWED_SORT = ("created_at", "id", "severity", "status", "action")
ALLOWED_ORDER = ("asc", "desc")
MAX_LIMIT = 200
DEFAULT_LIMIT = 50

AUDIT_SERVICE_KEY = os.getenv("AUDIT_SERVICE_KEY", "").strip()  # hiari, kwa maombi ya ndani ya huduma
ALLOW_DELETE = os.getenv("AUDIT_DELETE_ENABLED", "false").lower() in {"1","true","yes","y","on"}

# --------- Helpers ----------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _clamp_limit(limit: Optional[int]) -> int:
    if not limit:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))

def _order_by_whitelist(model, sort_by: str, order: str):
    key = sort_by if sort_by in ALLOWED_SORT else "created_at"
    col = getattr(model, key)
    return col.asc() if order == "asc" else col.desc()

def _asdict(obj: Any) -> Dict[str, Any]:
    d = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    # ensure meta is dict
    mv = d.get("meta")
    if isinstance(mv, (bytes, bytearray)):
        try:
            d["meta"] = json.loads(mv.decode("utf-8"))
        except Exception:
            d["meta"] = {}
    elif isinstance(mv, str):
        try:
            d["meta"] = json.loads(mv) if mv.strip() else {}
        except Exception:
            d["meta"] = {}
    elif mv is None:
        d["meta"] = {}
    return d

def _to_out(obj: Any) -> AuditLogOut:
    data = _asdict(obj)
    # Pydantic v2/v1 support
    if hasattr(AuditLogOut, "model_validate"):
        return AuditLogOut.model_validate(data)  # type: ignore
    return AuditLogOut(**data)  # type: ignore

def _compute_etag(obj: Any) -> str:
    ts = getattr(obj, "updated_at", None) or getattr(obj, "created_at", None)
    if isinstance(ts, datetime):
        base = str(int(ts.replace(tzinfo=timezone.utc).timestamp()))
    else:
        raw = f"{getattr(obj, 'id', '')}-{getattr(obj, 'action', '')}-{getattr(obj, 'status','')}"
        base = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f'W/"{base}"'

def _cursor_next(items: List[Any]) -> Optional[int]:
    if not items:
        return None
    return int(getattr(items[-1], "id", 0)) or None

def _apply_list_headers(resp: Response, *, total: Optional[int], limit: int, offset: int, cursor_next: Optional[int]):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Limit"] = str(limit)
    resp.headers["X-Offset"] = str(offset)
    if total is not None:
        resp.headers["X-Total-Count"] = str(total)
    if cursor_next:
        resp.headers["X-Cursor-Next"] = str(cursor_next)

# ======================================================================
# Public helper: emit_audit Ã¢â‚¬â€ tumia kwenye routes zako nyingine
# ======================================================================
def emit_audit(
    db: Session,
    *,
    action: str,
    status: str = "success",
    severity: str = "info",
    actor_id: Optional[int] = None,
    actor_email: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> AuditLogModel:
    inst = AuditLogModel()
    for k, v in {
        "action": action,
        "status": status,
        "severity": severity,
        "actor_id": actor_id,
        "actor_email": actor_email,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "ip": ip,
        "user_agent": user_agent,
        "meta": meta or {},
    }.items():
        if hasattr(inst, k):
            setattr(inst, k, v)
    if hasattr(inst, "created_at"):
        inst.created_at = _utcnow()
    if hasattr(inst, "updated_at"):
        inst.updated_at = _utcnow()

    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst

# ======================================================================
# LIST: /audit/logs  (filters + sorting + pagination)
# ======================================================================
@router.get(
    "/logs",
    response_model=List[AuditLogOut],
    summary="List audit logs (filter, search, paginate, sort)"
)
def list_audit_logs(
    response: Response,
    db: Session = Depends(get_db),
    # Filters
    q: Optional[str] = Query(None, description="Search in action/resource/status/meta"),
    user_id: Optional[int] = Query(None, alias="actor_id"),
    actor_email: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    status_f: Optional[str] = Query(None, alias="status"),
    severity: Optional[str] = Query(None),
    resource_type: Optional[str] = Query(None),
    resource_id: Optional[str] = Query(None),
    ip: Optional[str] = Query(None),
    created_from: Optional[datetime] = Query(None),
    created_to: Optional[datetime] = Query(None),
    # Sort & paginate
    sort_by: str = Query("created_at"),
    order: str = Query("desc"),
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    cursor: Optional[int] = Query(None),
    with_count: bool = Query(False),
):
    limit = _clamp_limit(limit)
    qy = db.query(AuditLogModel)

    # filters
    if user_id and hasattr(AuditLogModel, "actor_id"):
        qy = qy.filter(AuditLogModel.actor_id == user_id)
    if actor_email and hasattr(AuditLogModel, "actor_email"):
        qy = qy.filter(func.lower(AuditLogModel.actor_email) == actor_email.strip().lower())
    if action and hasattr(AuditLogModel, "action"):
        qy = qy.filter(AuditLogModel.action == action)
    if status_f and hasattr(AuditLogModel, "status"):
        qy = qy.filter(AuditLogModel.status == status_f)
    if severity and hasattr(AuditLogModel, "severity"):
        qy = qy.filter(AuditLogModel.severity == severity)
    if resource_type and hasattr(AuditLogModel, "resource_type"):
        qy = qy.filter(AuditLogModel.resource_type == resource_type)
    if resource_id and hasattr(AuditLogModel, "resource_id"):
        qy = qy.filter(AuditLogModel.resource_id == resource_id)
    if ip and hasattr(AuditLogModel, "ip"):
        qy = qy.filter(AuditLogModel.ip == ip)

    if created_from and hasattr(AuditLogModel, "created_at"):
        qy = qy.filter(AuditLogModel.created_at >= created_from)
    if created_to and hasattr(AuditLogModel, "created_at"):
        qy = qy.filter(AuditLogModel.created_at <= created_to)

    if q:
        like = f"%{q.strip()}%"
        conds = []
        for field in ("action", "resource_type", "resource_id", "status", "severity", "actor_email"):
            if hasattr(AuditLogModel, field):
                conds.append(getattr(AuditLogModel, field).ilike(like))
        if hasattr(AuditLogModel, "meta"):
            try:
                # Postgres JSONB ilike? kama sio, rudia kwa text cast ikiwa upo
                conds.append(func.cast(AuditLogModel.meta, func.TEXT).ilike(like))  # type: ignore
            except Exception:
                pass
        if conds:
            qy = qy.filter(or_(*conds))

    qy = qy.order_by(_order_by_whitelist(AuditLogModel, sort_by, order))

    total = None
    if with_count:
        total = qy.with_entities(func.count(AuditLogModel.id)).scalar() or 0

    # Cursor pagination
    if cursor and hasattr(AuditLogModel, "id"):
        if order == "desc":
            qy = qy.filter(AuditLogModel.id < cursor)
        else:
            qy = qy.filter(AuditLogModel.id > cursor)
        rows = qy.limit(limit).all()
        off = 0
    else:
        rows = qy.offset(offset).limit(limit).all()
        off = offset

    _apply_list_headers(response, total=total, limit=limit, offset=off, cursor_next=_cursor_next(rows) if order == "desc" else None)
    return [_to_out(r) for r in rows]

# ======================================================================
# GET ONE: /audit/logs/{id}
# ======================================================================
@router.get(
    "/logs/{log_id}",
    response_model=AuditLogOut,
    summary="Get single audit log by id"
)
def get_audit_log(
    log_id: int = Path(..., ge=1),
    response: Response = None,
    db: Session = Depends(get_db),
):
    row = db.query(AuditLogModel).filter(AuditLogModel.id == log_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Audit log not found")
    if response:
        response.headers["ETag"] = _compute_etag(row)
        response.headers["Cache-Control"] = "no-store"
    return _to_out(row)

# ======================================================================
# CREATE: /audit/logs  (admin OR service key)
# ======================================================================
@router.post(
    "/logs",
    response_model=AuditLogOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create audit log (admin or service)"
)
def create_audit_log(
    payload: AuditLogCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
    _: None = Depends(admin_guard),  # admin by default
):
    # Optional service-bypass: if service key matches, allow without admin_guard
    if AUDIT_SERVICE_KEY and x_service_key and x_service_key.strip() == AUDIT_SERVICE_KEY:
        pass  # allow
    # else admin_guard has already enforced admin

    data = payload.dict()
    # enrich ip/ua if missing
    if not data.get("ip") and request.client:
        data["ip"] = request.client.host
    if not data.get("user_agent"):
        data["user_agent"] = request.headers.get("user-agent")

    row = emit_audit(db, **data)
    response.headers["ETag"] = _compute_etag(row)
    response.headers["Cache-Control"] = "no-store"
    return _to_out(row)

# ======================================================================
# EXPORT: /audit/export?format=ndjson|csv (admin)
# ======================================================================
@router.get(
    "/export",
    summary="Export audit logs (NDJSON/CSV, streaming)",
)
def export_audit_logs(
    db: Session = Depends(get_db),
    _: None = Depends(admin_guard),
    fmt: str = Query("ndjson", pattern="ndjson|csv"),
    limit: int = Query(5000, ge=1, le=100000),
):
    qy = db.query(AuditLogModel).order_by(AuditLogModel.id.asc()).limit(limit)
    rows = qy.all()

    if fmt == "ndjson":
        async def gen_ndjson() -> AsyncGenerator[bytes, None]:
            for r in rows:
                yield (json.dumps(_asdict(r), ensure_ascii=False) + "\n").encode("utf-8")
        return StreamingResponse(gen_ndjson(), media_type="application/x-ndjson")

    # csv
    headers = [
        "id","created_at","action","status","severity",
        "actor_id","actor_email","resource_type","resource_id","ip","user_agent","meta"
    ]
    def gen_csv():
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        for r in rows:
            d = _asdict(r)
            writer.writerow([
                d.get("id"), d.get("created_at"),
                d.get("action"), d.get("status"), d.get("severity"),
                d.get("actor_id"), d.get("actor_email"),
                d.get("resource_type"), d.get("resource_id"),
                d.get("ip"), d.get("user_agent"),
                json.dumps(d.get("meta") or {}, ensure_ascii=False),
            ])
        yield buf.getvalue()
    return StreamingResponse(gen_csv(), media_type="text/csv; charset=utf-8")

# ======================================================================
# STATS: /audit/stats (admin)
# ======================================================================
@router.get(
    "/stats",
    response_model=AuditLogStats,
    summary="Audit stats by action/status/severity (admin)"
)
def audit_stats(
    db: Session = Depends(get_db),
    _: None = Depends(admin_guard),
):
    by_action = dict(db.query(AuditLogModel.action, func.count(AuditLogModel.id)).group_by(AuditLogModel.action).all())
    by_status = dict(db.query(AuditLogModel.status, func.count(AuditLogModel.id)).group_by(AuditLogModel.status).all())
    by_severity = dict(db.query(AuditLogModel.severity, func.count(AuditLogModel.id)).group_by(AuditLogModel.severity).all())
    payload = {"by_action": by_action, "by_status": by_status, "by_severity": by_severity}
    # Pydantic v2/v1 friendly
    if hasattr(AuditLogStats, "model_validate"):
        return AuditLogStats.model_validate(payload)  # type: ignore
    return AuditLogStats(**payload)  # type: ignore

# ======================================================================
# PURGE (Retention): /audit/purge?older_than_days=90 (admin)
# ======================================================================
@router.delete(
    "/purge",
    summary="Purge old audit logs by retention (admin)"
)
def purge_old(
    older_than_days: int = Query(90, ge=1, le=3650),
    db: Session = Depends(get_db),
    _: None = Depends(admin_guard),
):
    if not ALLOW_DELETE:
        raise HTTPException(status_code=403, detail="Delete/purge disabled by config")
    if not hasattr(AuditLogModel, "created_at"):
        raise HTTPException(status_code=400, detail="Model has no created_at for retention")
    cutoff = _utcnow() - timedelta(days=int(older_than_days))
    try:
        deleted = db.query(AuditLogModel).filter(AuditLogModel.created_at < cutoff).delete(synchronize_session=False)
        db.commit()
        return {"deleted": int(deleted), "cutoff": cutoff.isoformat()}
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to purge logs")


