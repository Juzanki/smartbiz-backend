# backend/dependencies.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from typing import Optional, Sequence, Set

from fastapi import Depends, HTTPException, status

# Tunategemea helper wako wa uthibitisho uliopo tayari
from backend.auth import get_current_user
from backend.models.user import User

logger = logging.getLogger(__name__)


# ------------- Helpers -------------
def _norm(s: Optional[str]) -> Optional[str]:
    return s.lower().strip() if isinstance(s, str) else None

def _user_plan(u: User) -> Optional[str]:
    """
    Ruhusu majina tofauti ya field ya mpango kulingana na model:
    subscription_status | plan | subscription_plan
    """
    for attr in ("subscription_status", "plan", "subscription_plan"):
        val = getattr(u, attr, None)
        if val:
            return _norm(str(val))
    return None

def _is_admin(u: User) -> bool:
    role = _norm(getattr(u, "role", None))
    if role in {"admin", "superadmin"}:
        return True
    return bool(getattr(u, "is_admin", False))

def _is_owner(u: User) -> bool:
    role = _norm(getattr(u, "role", None))
    if role == "owner":
        return True
    return bool(getattr(u, "is_owner", False))


# ------------- Plan guard -------------
def require_plan(allowed_plans: Sequence[str]):
    """
    Tumia kama dependency: @router.get(..., dependencies=[Depends(require_plan(["pro","business"]))])
    """
    allowed: Set[str] = {_norm(p) for p in allowed_plans if p}
    if not allowed:
        raise ValueError("require_plan() needs at least one allowed plan")

    async def checker(user: User = Depends(get_current_user)) -> User:
        plan = _user_plan(user)
        if plan not in allowed:
            logger.warning(
                "Plan access denied user=%s plan=%s allowed=%s",
                getattr(user, "email", None), plan, sorted(allowed),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: your subscription plan does not include this feature.",
            )
        logger.info("Plan access granted user=%s plan=%s", getattr(user, "email", None), plan)
        return user

    return checker


# ------------- Admin guard -------------
async def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if not _is_admin(current_user):
        logger.warning(
            "Admin access denied user=%s role=%s",
            getattr(current_user, "email", None),
            getattr(current_user, "role", None),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required.")
    logger.info("Admin access granted user=%s", getattr(current_user, "email", None))
    return current_user

# Alias kwa code ya zamani
async def check_admin(current_user: User = Depends(get_current_user)) -> User:
    return await get_admin_user(current_user)


# ------------- Owner guard -------------
async def get_owner_user(current_user: User = Depends(get_current_user)) -> User:
    if not _is_owner(current_user):
        logger.warning(
            "Owner access denied user=%s role=%s",
            getattr(current_user, "email", None),
            getattr(current_user, "role", None),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner access only.")
    return current_user

# Aliases kulingana na majina yaliyokuwa yakitumika
get_current_active_owner = get_owner_user   # alias
check_owner_only = get_owner_user           # alias


__all__ = [
    "require_plan",
    "get_admin_user",
    "check_admin",
    "get_owner_user",
    "get_current_active_owner",
    "check_owner_only",
]
