from __future__ import annotations
# backend/routes/register.py
"""Business user registration route for SmartBiz Assistant."""
from uuid import uuid4
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from backend.db import get_db
from backend.models.user import User
from backend.utils.security import get_password_hash

# Router ina prefix yake; kama main.py pia unaweka prefix, ondoa prefix huko.
router = APIRouter(prefix="/register-user", tags=["Register"])


# ---------- Schemas ----------

class RegisterRequest(BaseModel):
    """
    Input kwa usajili wa mtumiaji wa biashara.
    """
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=128)
    phone_number: str = Field(..., min_length=7, max_length=20)
    full_name: Optional[str] = Field(default="Guest", max_length=100)
    business_name: str = Field(..., min_length=2, max_length=120)
    business_type: str = Field(..., min_length=2, max_length=60)
    language: str = Field(..., min_length=2, max_length=10)
    telegram_id: Optional[int] = None

    class Config:  # pydantic v1 compat
        anystr_strip_whitespace = True
        validate_assignment = True
        extra = "forbid"


class RegisterResponse(BaseModel):
    user_token: str
    message: str


# ---------- Helpers ----------

def _normalize_phone(p: str) -> str:
    # Ondoa nafasi/viambishi visivyo hitajika; ruhusu + kianzio
    p = p.strip()
    if p.startswith("+"):
        head = "+"
        tail = "".join(ch for ch in p[1:] if ch.isdigit())
        return head + tail
    return "".join(ch for ch in p if ch.isdigit())


# ---------- Route ----------

@router.post(
    "/",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new business user",
)
def register_user(payload: RegisterRequest, db: Session = Depends(get_db)) -> RegisterResponse:
    """
    - Hulazimisha **unikia** kwa `username`, `email`, `phone_number`, na `telegram_id` (kama ipo).
    - Huhifadhi password kama **hash**.
    - Hutoa `user_token` ya kipekee kwa mteja.
    """
    # Normalize input
    username = payload.username.strip()
    email = payload.email.lower().strip()
    phone = _normalize_phone(payload.phone_number)

    # Pre-checks: duplicates (haraka kutoa ujumbe mzuri; bado tunategemea unique constraints DB)
    if db.query(User).filter(User.username.ilike(username)).first():
        raise HTTPException(status_code=409, detail="Username already registered.")

    if db.query(User).filter(User.email.ilike(email)).first():
        raise HTTPException(status_code=409, detail="Email already registered.")

    if db.query(User).filter(User.phone_number == phone).first():
        raise HTTPException(status_code=409, detail="Phone number already registered.")

    if payload.telegram_id is not None:
        if db.query(User).filter(User.telegram_id == payload.telegram_id).first():
            raise HTTPException(status_code=409, detail="Telegram ID already registered.")

    # Create token & hash
    user_token = str(uuid4())
    hashed_pw = get_password_hash(payload.password)

    # Build model
    new_user = User(
        username=username,
        email=email,
        full_name=(payload.full_name or "Guest").strip(),
        password=hashed_pw,
        telegram_id=payload.telegram_id,
        business_name=payload.business_name.strip(),
        business_type=payload.business_type.strip(),
        language=payload.language.strip(),
        phone_number=phone,
        user_token=user_token,
        subscription_status="free",
        subscription_expiry=datetime.utcnow(),  # unaweza kubadilisha > now + timedelta(days=14)
        created_at=datetime.utcnow(),
    )

    # Persist
    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
    except IntegrityError:
        # Ikiwa unique constraint imepigwa upande wa DB, rudi na 409 badala ya 500
        db.rollback()
        raise HTTPException(status_code=409, detail="Account with given details already exists.")
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Registration failed due to server error.")

    return RegisterResponse(
        user_token=user_token,
        message="Business registered successfully.",
    )

