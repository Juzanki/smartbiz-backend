from __future__ import annotations
# backend/routes/profile.py
"""
Routes for managing the authenticated user's profile in SmartBiz Assistant.

- GET   /profile/me    -> Read current profile
- PATCH /profile/me    -> Partially update current profile
- PUT   /profile/me    -> Full update (calls the same handler as PATCH)
- DELETE /profile/me   -> Delete current account

Security:
- Verifies token is NOT blacklisted (after /logout).
- Uses a flexible import for `get_current_user` (auth layout-agnostic).
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from backend.db import get_db
from backend.schemas import UserUpdate, User
from backend.schemas.user import UserOut
from backend.models.user import User as UserModel
try:
    from backend.auth import get_current_user  # type: ignore
except Exception:  # pragma: no cover
    try:
        from backend.routes.auth_routes import get_current_user  # type: ignore
    except Exception:  # pragma: no cover
        # Fail early with a clear message if nothing is found
        raise ImportError(
            "get_current_user not found. Ensure it exists in backend.auth or backend.routes.auth_routes."
        )

# Enforce logout blacklist (if you added the improved logout route)
try:
    from backend.routes.logout import verify_not_blacklisted
except Exception:  # pragma: no cover
    # Fallback no-op dependency if logout route not present yet
    def verify_not_blacklisted(token: str = "") -> str:  # type: ignore
        return token

logger = logging.getLogger("smartbiz.profile")
router = APIRouter(prefix="/profile", tags=["Profile"])


# ---------------------------- Helpers ----------------------------

def _strip(value: Optional[str]) -> Optional[str]:
    return value.strip() if isinstance(value, str) else value


def _normalize_language(lang: Optional[str]) -> Optional[str]:
    if not lang:
        return lang
    lang = lang.strip().lower().replace("_", "-")
    # add a tiny allowlist if unataka (posho ya baadaye)
    return lang


# ---------------------------- Routes ----------------------------

@router.get(
    "/me",
    response_model=UserOut,
    summary="Get current user profile",
)
def read_current_user(
    _token: str = Depends(verify_not_blacklisted),
    current_user: UserModel = Depends(get_current_user),
):
    """Rudisha taarifa za mtumiaji aliye-authenticate."""
    return current_user


@router.patch(
    "/me",
    response_model=UserOut,
    summary="Partially update current user profile",
)
def patch_current_user(
    update_data: UserUpdate,
    db: Session = Depends(get_db),
    _token: str = Depends(verify_not_blacklisted),
    current_user: UserModel = Depends(get_current_user),
):
    """
    Sasisha sehemu ya taarifa za mtumiaji. Inadumisha uadilifu:
    - Haimruhusu kubadilisha `username`, `email`, `password`, n.k. (UserUpdate inapaswa kudhibiti hili).
    - Huzuia duplicate ya `telegram_id` endapo inawekwa/updatiwa.
    """
    # Attach instance to session (kama ilikua detached)
    user = db.merge(current_user)

    # Tumia fields zilizotumwa tu
    # (UserUpdate inapaswa kuwa na fields: full_name, business_name, business_type, language, telegram_id, n.k.)
    changes = update_data.model_dump(exclude_unset=True)  # Pydantic v2
    # kwa pydantic v1: update_data.dict(exclude_unset=True)

    # Sanitize/normalize baadhi ya fields
    if "full_name" in changes:
        user.full_name = _strip(changes["full_name"])
    if "business_name" in changes:
        user.business_name = _strip(changes["business_name"])
    if "business_type" in changes:
        user.business_type = _strip(changes["business_type"])
    if "language" in changes:
        user.language = _normalize_language(changes["language"])

    # Protect against duplicate telegram_id (ikiwa imetumwa na si None)
    if "telegram_id" in changes and changes["telegram_id"] is not None:
        new_tid = changes["telegram_id"]
        exists = (
            db.query(UserModel)
            .filter(UserModel.telegram_id == new_tid, UserModel.id != user.id)
            .first()
        )
        if exists:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Telegram ID already in use by another account.",
            )
        user.telegram_id = new_tid

    try:
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        # Safety net in case DB unique constraints zingegonga
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Update violates a unique constraint.",
        )
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update profile.",
        )

    logger.info("User %s updated own profile.", user.id)
    return user


# PUT -> tumia handler ya PATCH ili kuepuka code duplication
@router.put(
    "/me",
    response_model=UserOut,
    summary="Update current user profile (full)",
)
def put_current_user(
    update_data: UserUpdate,
    db: Session = Depends(get_db),
    _token: str = Depends(verify_not_blacklisted),
    current_user: UserModel = Depends(get_current_user),
):
    return patch_current_user(
        update_data=update_data,
        db=db,
        _token=_token,
        current_user=current_user,
    )


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete current user profile",
)
def delete_current_user(
    db: Session = Depends(get_db),
    _token: str = Depends(verify_not_blacklisted),
    current_user: UserModel = Depends(get_current_user),
):
    """
    Futa akaunti ya sasa kabisa.
    NOTE: 204 hairudishi mwili wa response.
    """
    user = db.merge(current_user)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    try:
        db.delete(user)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete profile.",
        )

    # 204 â€“ no content
    return Response(status_code=status.HTTP_204_NO_CONTENT)


