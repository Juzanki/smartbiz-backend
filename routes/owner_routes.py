# backend/routes/owner_routes.py
# -*- coding: utf-8 -*-
"""Owner-only administration routes for SmartBiz Assistance."""

from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ConfigDict, EmailStr
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from backend.db import get_db
from backend.dependencies import check_owner_only, get_current_active_owner
from backend.models.user import User
from backend.schemas import RoleUpdateRequest, AdminCreate
from backend.utils.security import pwd_context

# Router
router = APIRouter(prefix="/owner", tags=["Owner"])

# ---------------- DTOs ----------------
class MessageOut(BaseModel):
    """Standard message response."""
    detail: str

class AdminBrief(BaseModel):
    id: int
    email: EmailStr
    name: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class AdminList(BaseModel):
    items: List[AdminBrief]
    total: int
    limit: int
    offset: int

# ---------------- Helpers ----------------
ALLOWED_UPDATE_ROLES = {"user", "admin"}  # owner cannot be assigned via API

def _display_name(user: User) -> str:
    """Return the best available display name for a user."""
    return getattr(user, "full_name", None) or getattr(user, "name", None) or getattr(user, "username", None) or user.email

def _set_name(user: User, name: str) -> None:
    """Set the display name on the user model."""
    if hasattr(user, "full_name"):
        setattr(user, "full_name", name)
    elif hasattr(user, "name"):
        setattr(user, "name", name)

def _set_password_hash(user: User, raw_password: str) -> None:
    """Hash and set the password for the user."""
    hashed = pwd_context.hash(raw_password)
    if hasattr(user, "password_hash"):
        setattr(user, "password_hash", hashed)
    elif hasattr(user, "password"):
        setattr(user, "password", hashed)
    else:
        raise HTTPException(status_code=500, detail="User model missing password field")

def _is_owner(user: User) -> bool:
    """Check if the given user has the 'owner' role."""
    return getattr(user, "role", None) == "owner"

# ---------------- Routes ----------------
@router.get("/dashboard", summary="Owner-only dashboard")
def owner_dashboard(current_user: User = Depends(check_owner_only)):
    """Return a welcome message and role info for the owner."""
    return {
        "message": f"Welcome, Owner: {_display_name(current_user)} ({current_user.email})",
        "role": current_user.role,
        "note": "You have full system control."
    }

@router.post("/update-role", response_model=MessageOut, summary="Owner can update user roles")
def update_user_role(
    payload: RoleUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_owner_only),
):
    """Update the role of a user (owner can only set to 'user' or 'admin')."""
    user = db.query(User).filter(User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    new_role = payload.new_role.strip().lower()
    if new_role not in ALLOWED_UPDATE_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role. Allowed: user, admin")

    if user.id == current_user.id and new_role != "owner":
        raise HTTPException(status_code=403, detail="You cannot change your own role via this endpoint")

    if _is_owner(user):
        raise HTTPException(status_code=403, detail="Cannot change role of an owner account")

    user.role = new_role
    db.commit()
    return MessageOut(detail=f"{user.email} is now a {user.role}")

@router.post(
    "/admins",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a new admin",
    dependencies=[Depends(get_current_active_owner)],
)
def add_admin(admin: AdminCreate, db: Session = Depends(get_db)):
    """Create a new admin account."""
    email_norm = admin.email.strip().lower()
    exists = db.query(User).filter(func.lower(User.email) == email_norm).first()
    if exists:
        raise HTTPException(status_code=409, detail="Email already in use")

    if len(admin.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters long")

    new_user = User(email=email_norm, role="admin")
    _set_name(new_user, admin.name.strip())
    _set_password_hash(new_user, admin.password)

    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return MessageOut(detail=f"Admin '{new_user.email}' created successfully")

@router.get("/admins", response_model=AdminList, summary="List all admins (owner only)", dependencies=[Depends(get_current_active_owner)])
def list_admins(
    db: Session = Depends(get_db),
    q: Optional[str] = Query(None, description="Search by email or name"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Return a paginated list of all admins."""
    base = db.query(User).filter(User.role == "admin")
    if q:
        qs = f"%{q.strip()}%"
        conds = [User.email.ilike(qs)]
        if hasattr(User, "full_name"):
            conds.append(User.full_name.ilike(qs))
        if hasattr(User, "name"):
            conds.append(User.name.ilike(qs))
        base = base.filter(or_(*conds))

    total = base.count()
    if hasattr(User, "created_at"):
        base = base.order_by(User.created_at.desc())
    else:
        base = base.order_by(User.id.desc())

    rows = base.offset(offset).limit(limit).all()
    items = [AdminBrief(id=u.id, email=u.email, name=_display_name(u)) for u in rows]
    return AdminList(items=items, total=total, limit=limit, offset=offset)

@router.get("/legacy/admins", response_model=List[AdminBrief], summary="(Legacy) List admins")
def list_admins_legacy(db: Session = Depends(get_db)):
    """Legacy endpoint to list all admins without pagination."""
    admins = db.query(User).filter(User.role == "admin").all()
    return [AdminBrief(id=a.id, email=a.email, name=_display_name(a)) for a in admins]
