# backend/routes/team.py
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Team Management API (mobile-first, international-ready)

Endpoints
- GET    /team/members                               -> list members (filters + cursor pagination)
- GET    /team/members/{member_id}                   -> get single member
- POST   /team/members                                -> add/invite a member (idempotent)
- PATCH  /team/members/{member_id}                    -> partial update (name, role, status, phone, etc.)
- POST   /team/members/{member_id}/role               -> quick role change
- POST   /team/members/{member_id}/status             -> quick status change (activate/suspend)
- POST   /team/invite                                 -> invite by email (idempotent; optional team_crud)
- POST   /team/members/{member_id}/resend-invite      -> resend invite (optional team_crud)
- DELETE /team/members/{member_id}                    -> remove (soft if supported, else hard)
- GET    /team/stats                                  -> quick counts per status/role

Notes
- Uses your existing models/schemas. If a field doesn't exist in your model (e.g., status),
  the code gracefully skips it.
- Optional team_crud hooks are used if present; otherwise a safe ORM fallback is used.
- Mobile-first: tiny payloads, cursor pagination, and ISO UTC timestamps.
"""
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Header,
    Query,
    status,
)
from pydantic import BaseModel, Field, conint, constr
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models import TeamMember
from backend.models.user import User
from backend.schemas import TeamMemberCreate, TeamMemberUpdate, TeamMemberOut
from backend.dependencies import get_current_user

# Optional richer CRUD if you have it
try:
    from backend.crud import team_crud  # type: ignore
except Exception:  # pragma: no cover
    team_crud = None  # type: ignore

router = APIRouter(prefix="/team", tags=["Team Management"])

# ---------- mobile-first defaults ----------
DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100

# ---------- helpers ----------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if isinstance(dt, datetime) else None

def _norm_email(email: str) -> str:
    e = (email or "").strip().casefold()
    # collapse consecutive dots in local part, trim spaces
    return re.sub(r"\s+", "", e)

def _norm_name(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80]

def _owner_scope(q, owner_id: int):
    # standard owner scoping
    return q.filter(TeamMember.owner_id == owner_id)

def _hasattr_safe(obj: Any, name: str) -> bool:
    try:
        return hasattr(obj, name)
    except Exception:
        return False

# ---------- tiny local schemas for extra endpoints ----------
class PageMeta(BaseModel):
    next_cursor: Optional[int] = None
    count: int

class MemberPageOut(BaseModel):
    meta: PageMeta
    items: List[TeamMemberOut]

class RoleChangeIn(BaseModel):
    role: Literal["viewer", "editor", "admin", "owner"]

class StatusChangeIn(BaseModel):
    status: Literal["invited", "active", "suspended", "removed"]

class InviteIn(BaseModel):
    email: constr(min_length=3, max_length=120)
    full_name: Optional[constr(max_length=80)] = None
    role: Literal["viewer", "editor", "admin"] = "viewer"
    phone: Optional[constr(max_length=32)] = None

class OkResponse(BaseModel):
    ok: bool = True
    message: Optional[str] = None

# ---------- list members ----------
@router.get(
    "/members",
    response_model=MemberPageOut,
    summary="List team members (filters + cursor pagination)",
)
def get_team_members(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    cursor_id: Optional[int] = Query(None, description="Paginate backward: id < cursor_id"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    q: Optional[str] = Query(None, max_length=80, description="Search by name or email"),
    role: Optional[str] = Query(None, description="Filter by role"),
    status_eq: Optional[str] = Query(None, description="Filter by status"),
) -> MemberPageOut:
    # Prefer team_crud if available
    if team_crud and hasattr(team_crud, "list_members"):
        result = team_crud.list_members(
            db,
            owner_id=current_user.id,
            cursor_id=cursor_id,
            limit=limit,
            q=q,
            role=role,
            status_eq=status_eq,
        )
        items = result.get("items", [])
        next_cursor = result.get("next_cursor")
        return MemberPageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=items)

    query = _owner_scope(db.query(TeamMember), current_user.id)
    if q:
        like = f"%{q.strip()}%"
        # Try to search both name and email if columns exist
        conds = []
        if _hasattr_safe(TeamMember, "full_name"):
            conds.append(TeamMember.full_name.ilike(like))
        conds.append(TeamMember.email.ilike(like))
        query = query.filter(*conds) if len(conds) == 1 else query.filter((conds[0]) | (conds[1]))
    if role and _hasattr_safe(TeamMember, "role"):
        query = query.filter(TeamMember.role == role)
    if status_eq and _hasattr_safe(TeamMember, "status"):
        query = query.filter(TeamMember.status == status_eq)
    if cursor_id:
        query = query.filter(TeamMember.id < cursor_id)

    rows = query.order_by(TeamMember.id.desc()).limit(limit).all()
    next_cursor = rows[-1].id if rows else None
    return MemberPageOut(
        meta=PageMeta(next_cursor=next_cursor, count=len(rows)),
        items=[TeamMemberOut.model_validate(r) for r in rows],
    )

# ---------- get one ----------
@router.get(
    "/members/{member_id}",
    response_model=TeamMemberOut,
    summary="Get a single team member",
)
def get_team_member(
    member_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    r = _owner_scope(db.query(TeamMember), current_user.id).filter(TeamMember.id == member_id).one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Team member not found")
    return TeamMemberOut.model_validate(r)

# ---------- add (create/invite) ----------
@router.post(
    "/members",
    response_model=TeamMemberOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add or invite a team member (idempotent)",
)
def add_team_member(
    payload: TeamMemberCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(
        None, convert_underscores=False, description="Optional key to prevent duplicates"
    ),
):
    # Normalize inputs where present
    data = payload.model_dump(exclude_unset=True)
    if "email" in data:
        data["email"] = _norm_email(data["email"])
    if "full_name" in data:
        data["full_name"] = _norm_name(data.get("full_name"))

    # Prefer richer CRUD if available
    if team_crud and hasattr(team_crud, "create_member"):
        return team_crud.create_member(
            db,
            owner_id=current_user.id,
            payload=TeamMemberCreate(**data),
            idempotency_key=idempotency_key,
        )

    # Fallback ORM: unique per owner+email
    existing = (
        _owner_scope(db.query(TeamMember), current_user.id)
        .filter(TeamMember.email == data.get("email"))
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="Team member already exists")

    new_member = TeamMember(owner_id=current_user.id, **data)
    # default status if available
    if _hasattr_safe(TeamMember, "status") and getattr(new_member, "status", None) is None:
        new_member.status = "invited"
    if idempotency_key and _hasattr_safe(TeamMember, "invite_key"):
        new_member.invite_key = idempotency_key  # optional column for idempotency
    db.add(new_member)
    db.commit()
    db.refresh(new_member)
    return TeamMemberOut.model_validate(new_member)

# ---------- partial update ----------
@router.patch(
    "/members/{member_id}",
    response_model=TeamMemberOut,
    summary="Partially update a team member",
)
def patch_team_member(
    member_id: int,
    payload: TeamMemberUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    if_match: Optional[str] = Header(
        None, convert_underscores=False, description="Optional version/concurrency token if you add it"
    ),
):
    # Prefer CRUD if available
    if team_crud and hasattr(team_crud, "update_member"):
        m = team_crud.update_member(db, owner_id=current_user.id, member_id=member_id, payload=payload, if_match=if_match)
        if not m:
            raise HTTPException(status_code=404, detail="Team member not found")
        return m

    member = (
        _owner_scope(db.query(TeamMember), current_user.id)
        .filter(TeamMember.id == member_id)
        .with_for_update()
        .one_or_none()
    )
    if not member:
        raise HTTPException(status_code=404, detail="Team member not found")

    data = payload.model_dump(exclude_unset=True)
    if "email" in data:
        data["email"] = _norm_email(data["email"])
    if "full_name" in data:
        data["full_name"] = _norm_name(data.get("full_name"))

    for field, value in data.items():
        if _hasattr_safe(member, field):
            setattr(member, field, value)
    if _hasattr_safe(member, "updated_at"):
        member.updated_at = _utcnow()
    db.commit()
    db.refresh(member)
    return TeamMemberOut.model_validate(member)

# ---------- quick role/status toggles ----------
@router.post(
    "/members/{member_id}/role",
    response_model=TeamMemberOut,
    summary="Change member role quickly",
)
def change_role(
    member_id: int,
    body: RoleChangeIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if team_crud and hasattr(team_crud, "set_role"):
        m = team_crud.set_role(db, owner_id=current_user.id, member_id=member_id, role=body.role)
        if not m:
            raise HTTPException(status_code=404, detail="Team member not found")
        return m

    member = _owner_scope(db.query(TeamMember), current_user.id).filter(TeamMember.id == member_id).one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Team member not found")
    if not _hasattr_safe(member, "role"):
        raise HTTPException(status_code=501, detail="Role field not supported")
    member.role = body.role
    if _hasattr_safe(member, "updated_at"):
        member.updated_at = _utcnow()
    db.commit()
    db.refresh(member)
    return TeamMemberOut.model_validate(member)

@router.post(
    "/members/{member_id}/status",
    response_model=TeamMemberOut,
    summary="Change member status quickly",
)
def change_status(
    member_id: int,
    body: StatusChangeIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if team_crud and hasattr(team_crud, "set_status"):
        m = team_crud.set_status(db, owner_id=current_user.id, member_id=member_id, status=body.status)
        if not m:
            raise HTTPException(status_code=404, detail="Team member not found")
        return m

    member = _owner_scope(db.query(TeamMember), current_user.id).filter(TeamMember.id == member_id).one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Team member not found")
    if not _hasattr_safe(member, "status"):
        raise HTTPException(status_code=501, detail="Status field not supported")
    member.status = body.status
    if _hasattr_safe(member, "updated_at"):
        member.updated_at = _utcnow()
    db.commit()
    db.refresh(member)
    return TeamMemberOut.model_validate(member)

# ---------- invite & resend ----------
@router.post(
    "/invite",
    response_model=TeamMemberOut,
    summary="Invite a member by email (idempotent)",
)
def invite_member(
    body: InviteIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, convert_underscores=False),
):
    email = _norm_email(body.email)
    full_name = _norm_name(body.full_name)

    if team_crud and hasattr(team_crud, "invite_member"):
        return team_crud.invite_member(
            db,
            owner_id=current_user.id,
            email=email,
            full_name=full_name,
            role=body.role,
            phone=body.phone,
            idempotency_key=idempotency_key,
        )

    # Fallback: reuse POST /members flow via TeamMemberCreate
    payload = TeamMemberCreate(email=email, full_name=full_name, role=body.role, phone=body.phone)  # type: ignore[arg-type]
    return add_team_member(payload, db, current_user, idempotency_key)  # type: ignore[arg-type]

@router.post(
    "/members/{member_id}/resend-invite",
    response_model=OkResponse,
    summary="Resend an invitation email (noop if not supported)",
)
def resend_invite(
    member_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if team_crud and hasattr(team_crud, "resend_invite"):
        ok = team_crud.resend_invite(db, owner_id=current_user.id, member_id=member_id)
        return OkResponse(ok=bool(ok), message="Invite resent" if ok else "Nothing to send")
    # Non-fatal noop for fallback
    return OkResponse(ok=True, message="Invite resend not supported; no-op")

# ---------- remove (soft/hard) ----------
@router.delete(
    "/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a team member (soft if supported, else hard)",
)
def remove_team_member(
    member_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if team_crud and hasattr(team_crud, "remove_member"):
        ok = team_crud.remove_member(db, owner_id=current_user.id, member_id=member_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Team member not found")
        return

    member = _owner_scope(db.query(TeamMember), current_user.id).filter(TeamMember.id == member_id).first()
    if not member:
        raise HTTPException(status_code=404, detail="Team member not found")

    # Soft delete if supported
    if _hasattr_safe(member, "status"):
        try:
            member.status = "removed"
            if _hasattr_safe(member, "updated_at"):
                member.updated_at = _utcnow()
            db.commit()
            return
        except Exception:
            db.rollback()
    # Hard delete fallback
    db.delete(member)
    db.commit()
    return

# ---------- quick stats ----------
@router.get(
    "/stats",
    summary="Quick team stats (counts by status/role)",
)
def team_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if team_crud and hasattr(team_crud, "stats"):
        return team_crud.stats(db, owner_id=current_user.id)

    # Simple counts (fallback)
    base = _owner_scope(db.query(TeamMember), current_user.id)
    out: Dict[str, Any] = {"total": base.count()}
    if _hasattr_safe(TeamMember, "status"):
        for s in ["invited", "active", "suspended", "removed"]:
            out[s] = base.filter(TeamMember.status == s).count()
    if _hasattr_safe(TeamMember, "role"):
        for r in ["viewer", "editor", "admin", "owner"]:
            out[f"role_{r}"] = base.filter(TeamMember.role == r).count()
    return out

