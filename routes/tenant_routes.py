# backend/routes/tenants.py
# -*- coding: utf-8 -*-
"""
Tenants API (mobile-first, international-ready)

Endpoints
- POST   /tenants/                         -> create tenant (owner-only, idempotent)
- GET    /tenants/                         -> list tenants (owner-only; filters + cursor pagination)
- GET    /tenants/mine                     -> list current user's tenant(s)
- GET    /tenants/{tenant_id}              -> get a tenant (owner or member)
- PATCH  /tenants/{tenant_id}              -> partial update (owner-only; optimistic lock ready)
- POST   /tenants/{tenant_id}/activate     -> set status=active (owner-only)
- POST   /tenants/{tenant_id}/suspend      -> set status=suspended (owner-only)
- DELETE /tenants/{tenant_id}              -> remove tenant (soft if supported; owner-only)

Notes
- Prefers tenant_crud helpers when available; otherwise uses safe fallbacks.
- English-only code & docs. UTC ISO timestamps are recommended in your models.
"""
from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Header,
    Query,
    status,
)
from pydantic import BaseModel, Field, constr
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.schemas.tenant import TenantCreate, TenantOut
from backend.crud import tenant_crud

router = APIRouter(prefix="/tenants", tags=["Tenants"])

# ---------- mobile-first defaults ----------
DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100

# ---------- helpers ----------
def _is_owner(user: User) -> bool:
    return str(getattr(user, "role", "")).lower() == "owner"

def _require_owner(user: User) -> None:
    if not _is_owner(user):
        raise HTTPException(status_code=403, detail="Only the system owner can perform this action.")

def _norm_slug(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    s = s.strip().casefold()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or None

def _norm_name(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120]

def _dump_partial(model: Any) -> Dict[str, Any]:
    # Pydantic v2/v1 compatible partial dump
    try:
        return model.model_dump(exclude_unset=True)
    except AttributeError:
        return model.dict(exclude_unset=True)

# ---------- lightweight extra schemas ----------
class PageMeta(BaseModel):
    next_cursor: Optional[int] = None
    count: int

class TenantPageOut(BaseModel):
    meta: PageMeta
    items: List[TenantOut]

class TenantPatch(BaseModel):
    name: Optional[constr(min_length=2, max_length=120)] = None
    slug: Optional[constr(min_length=2, max_length=120)] = None
    status: Optional[Literal["active", "suspended", "deleted"]] = None
    plan: Optional[str] = Field(None, description="Optional plan code, if your model supports it")
    metadata: Optional[Dict[str, Any]] = None

class ActionResponse(BaseModel):
    ok: bool = True
    message: Optional[str] = None

# ---------- routes ----------

@router.post(
    "/",
    response_model=TenantOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tenant (owner-only, idempotent)",
)
def create_tenant(
    tenant: TenantCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(
        None, convert_underscores=False, description="Optional key to prevent duplicate creations"
    ),
):
    _require_owner(current_user)

    # Normalize common fields if present on your schema
    data = _dump_partial(tenant)
    if "name" in data:
        data["name"] = _norm_name(data["name"])
    if "slug" in data and data["slug"]:
        data["slug"] = _norm_slug(data["slug"])
    # Rebuild the payload with normalized values
    try:
        tenant = TenantCreate(**data)  # type: ignore[arg-type]
    except Exception:
        # If schema differs, best-effort mutation
        for k, v in data.items():
            if hasattr(tenant, k):
                setattr(tenant, k, v)

    # Prefer extended CRUD signature if available
    try:
        return tenant_crud.create_tenant(db, tenant, idempotency_key=idempotency_key)  # type: ignore[misc]
    except TypeError:
        return tenant_crud.create_tenant(db, tenant)


@router.get(
    "/",
    response_model=TenantPageOut,
    summary="List tenants (owner-only; filters + cursor pagination)",
)
def list_tenants(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    cursor_id: Optional[int] = Query(None, description="Paginate backward: id < cursor_id"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    q: Optional[str] = Query(None, max_length=80, description="Search by name or slug"),
    status_eq: Optional[str] = Query(None, description="Filter by exact status"),
    plan: Optional[str] = Query(None, description="Filter by plan code"),
):
    _require_owner(current_user)

    if hasattr(tenant_crud, "list_tenants"):
        result = tenant_crud.list_tenants(
            db,
            cursor_id=cursor_id,
            limit=limit,
            q=q,
            status_eq=status_eq,
            plan=plan,
        )
        items = result.get("items", [])
        next_cursor = result.get("next_cursor")
        return TenantPageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=items)

    # Fallback: use get_all_tenants then slice (no DB-level filters)
    rows = tenant_crud.get_all_tenants(db)
    if q:
        ql = q.strip().casefold()
        def _match(it: Any) -> bool:
            name = str(getattr(it, "name", "")).casefold()
            slug = str(getattr(it, "slug", "")).casefold()
            return ql in name or ql in slug
        rows = [r for r in rows if _match(r)]
    if cursor_id:
        rows = [r for r in rows if getattr(r, "id", 0) < cursor_id]
    items = rows[:limit]
    next_cursor = getattr(items[-1], "id", None) if items else None
    # Convert to schema if needed
    try:
        items = [TenantOut.model_validate(r) for r in items]  # type: ignore[assignment]
    except Exception:
        pass
    return TenantPageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=items)  # type: ignore[arg-type]


@router.get(
    "/mine",
    response_model=List[TenantOut],
    summary="Get the tenant(s) for the current user",
)
def my_tenants(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Prefer CRUD helper if available
    if hasattr(tenant_crud, "get_tenants_for_user"):
        return tenant_crud.get_tenants_for_user(db, user_id=current_user.id)

    # Common fallback patterns:
    # 1) If your User has tenant_id
    if hasattr(current_user, "tenant_id") and getattr(current_user, "tenant_id"):
        if hasattr(tenant_crud, "get_tenant"):
            t = tenant_crud.get_tenant(db, tenant_id=getattr(current_user, "tenant_id"))
            return [t] if t else []
        if hasattr(tenant_crud, "get_all_tenants"):
            all_ts = tenant_crud.get_all_tenants(db)
            return [t for t in all_ts if getattr(t, "id", None) == getattr(current_user, "tenant_id")]

    # 2) If your User has many-to-many membership, expose a CRUD later
    raise HTTPException(status_code=501, detail="Fetching user tenants is not supported by the current CRUD")


@router.get(
    "/{tenant_id}",
    response_model=TenantOut,
    summary="Get tenant details",
)
def get_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if hasattr(tenant_crud, "get_tenant"):
        t = tenant_crud.get_tenant(db, tenant_id=tenant_id, viewer_id=current_user.id)
        if not t:
            raise HTTPException(status_code=404, detail="Tenant not found")
        # Basic visibility: owner can view any, members can view their own tenant
        if not _is_owner(current_user):
            # If CRUD enforces visibility, we trust it; otherwise do a minimal check
            member_ok = False
            if hasattr(current_user, "tenant_id"):
                member_ok = (getattr(current_user, "tenant_id") == tenant_id)
            if not member_ok:
                # If your CRUD already filtered by viewer_id, you won't reach here
                raise HTTPException(status_code=403, detail="Not allowed to view this tenant")
        return t

    raise HTTPException(status_code=501, detail="get_tenant not implemented in tenant_crud")


@router.patch(
    "/{tenant_id}",
    response_model=TenantOut,
    summary="Partially update a tenant (owner-only)",
)
def update_tenant(
    tenant_id: int,
    patch: TenantPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    if_match: Optional[str] = Header(
        None, convert_underscores=False, description="Optional optimistic lock token (e.g., version)"
    ),
):
    _require_owner(current_user)

    data = _dump_partial(patch)
    if "name" in data:
        data["name"] = _norm_name(data["name"])
    if "slug" in data and data["slug"]:
        data["slug"] = _norm_slug(data["slug"])

    if hasattr(tenant_crud, "update_tenant"):
        return tenant_crud.update_tenant(db, tenant_id=tenant_id, patch=data, if_match=if_match)

    raise HTTPException(status_code=501, detail="update_tenant not implemented in tenant_crud")


@router.post(
    "/{tenant_id}/activate",
    response_model=ActionResponse,
    summary="Activate a tenant (owner-only)",
)
def activate_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_owner(current_user)
    if hasattr(tenant_crud, "set_status"):
        ok = tenant_crud.set_status(db, tenant_id=tenant_id, status="active")
        if not ok:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return ActionResponse(ok=True, message="Tenant activated")
    if hasattr(tenant_crud, "activate_tenant"):
        ok = tenant_crud.activate_tenant(db, tenant_id=tenant_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return ActionResponse(ok=True, message="Tenant activated")
    raise HTTPException(status_code=501, detail="Status change not supported by tenant_crud")


@router.post(
    "/{tenant_id}/suspend",
    response_model=ActionResponse,
    summary="Suspend a tenant (owner-only)",
)
def suspend_tenant(
    tenant_id: int,
    reason: Optional[str] = Query(None, max_length=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_owner(current_user)
    if hasattr(tenant_crud, "set_status"):
        ok = tenant_crud.set_status(db, tenant_id=tenant_id, status="suspended", reason=reason)
        if not ok:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return ActionResponse(ok=True, message="Tenant suspended")
    if hasattr(tenant_crud, "suspend_tenant"):
        ok = tenant_crud.suspend_tenant(db, tenant_id=tenant_id, reason=reason)
        if not ok:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return ActionResponse(ok=True, message="Tenant suspended")
    raise HTTPException(status_code=501, detail="Status change not supported by tenant_crud")


@router.delete(
    "/{tenant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a tenant (owner-only; soft if supported)",
)
def delete_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    hard_delete: bool = Query(False, description="If true, attempt hard delete when supported"),
):
    _require_owner(current_user)

    # Prefer explicit CRUDs if present
    if hasattr(tenant_crud, "delete_tenant") and hard_delete:
        ok = tenant_crud.delete_tenant(db, tenant_id=tenant_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return

    if hasattr(tenant_crud, "soft_delete_tenant") and not hard_delete:
        ok = tenant_crud.soft_delete_tenant(db, tenant_id=tenant_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return

    # Generic status fallback
    if hasattr(tenant_crud, "set_status"):
        ok = tenant_crud.set_status(db, tenant_id=tenant_id, status="deleted")
        if not ok:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return

    raise HTTPException(status_code=501, detail="Delete not supported by tenant_crud")
