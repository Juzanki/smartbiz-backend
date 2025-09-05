# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Forgot Password Flow for SmartBiz Assistance.
- Anti-enumeration: haturudishi 404 kwa "user not found" ili kuepuka kufichua akaunti.
- Cooldown: kama code bado haija-expire, hatutoi mpya (tunatoa muda uliobaki).
- Safe time handling: tunatumia UTC, aware comparisons.
- Consistent responses: rafiki kwa mobile (message, resend_after_seconds).
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import secrets

from backend.db import get_db
from backend.models.forgot_password import PasswordResetCode
from backend.schemas import ForgotPasswordRequest, VerifyResetCode, ResetPassword
from backend.utils.verification import generate_verification_code
from backend.crud import get_user_by_identifier
from backend.utils.security import get_password_hash  # if you have verify_password, you can hash+verify the code too.

router = APIRouter(prefix="/auth/forgot", tags=["Auth: Forgot Password"])

# ---- Config ----
CODE_TTL_MINUTES = 5  # muda wa kuishi kwa code
NOW = lambda: datetime.now(timezone.utc)


# ---- Response Models (mobile-friendly) ----
class MessageResponse(BaseModel):
    message: str = Field(..., example="Verification code sent successfully")


class SendCodeResponse(MessageResponse):
    # Ikiwa code ipo tayari (haija-expire), tunarudisha muda wa kusubiri kabla ya ku-resend
    resend_after_seconds: Optional[int] = Field(
        None, description="Seconds to wait before requesting another code"
    )


# ---- Helpers ----
def _get_active_code(db: Session, identifier: str) -> Optional[PasswordResetCode]:
    """Rudisha code isiyo-expire (kama ipo) kwa mtumiaji huyu."""
    return (
        db.query(PasswordResetCode)
        .filter(
            PasswordResetCode.identifier == identifier,
            PasswordResetCode.expires_at > NOW(),
        )
        .order_by(PasswordResetCode.expires_at.desc())
        .first()
    )


def _resend_wait_seconds(existing: PasswordResetCode) -> int:
    """Hesabu sekunde zilizobaki mpaka code iliyopo i-expire."""
    delta = existing.expires_at - NOW()
    return max(int(delta.total_seconds()), 0)


# ---- Endpoints ----
@router.post(
    "",
    summary="Send password reset code to user",
    response_model=SendCodeResponse,
    status_code=status.HTTP_200_OK,
)
def send_reset_code(payload: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """
    Generate and store a password reset code for a user using email or phone number.
    NOTE: Anti-enumeration â€” daima tunarudisha ujumbe wa mafanikio bila kufichua kama user yupo au la.
    """
    try:
        # 1) Thibitisha kimyakimya kama user yupo (bila kumfichua mteja)
        user = get_user_by_identifier(db, payload.identifier)

        # 2) Kama tayari kuna code hai (haija-expire), usitoe mpya â€” rudisha muda uliobaki
        existing = _get_active_code(db, payload.identifier)
        if existing:
            return SendCodeResponse(
                message="Verification code sent successfully",
                resend_after_seconds=_resend_wait_seconds(existing),
            )

        # 3) Ikiwa tunaruhusu kutoa mpya
        if user:
            code = generate_verification_code()
            expires_at = NOW() + timedelta(minutes=CODE_TTL_MINUTES)

            # (Optional, but safe) Futa codes zilizopitwa na wakati za identifier huyu
            db.query(PasswordResetCode).filter(
                PasswordResetCode.identifier == payload.identifier,
                PasswordResetCode.expires_at <= NOW(),
            ).delete(synchronize_session=False)

            reset_entry = PasswordResetCode(
                identifier=payload.identifier,
                code=code,              # âœ… TIP: Ikiwa unaweza, hifadhi HASH badala ya plaintext
                expires_at=expires_at,
            )
            db.add(reset_entry)
            db.commit()

            # TODO: Replace with real SMS/Email integration (async task/queue)
            # e.g., background_tasks.add_task(send_sms_or_email, payload.identifier, code)
            print(f"[DEBUG] Verification code for {payload.identifier}: {code}")

        # 4) Daima rudisha success (anti-enumeration)
        return SendCodeResponse(message="Verification code sent successfully")

    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to send reset code") from exc


@router.post(
    "/verify-reset-code",
    summary="Verify the password reset code",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
def verify_code(payload: VerifyResetCode, db: Session = Depends(get_db)):
    """
    Verify that the provided reset code matches and is not expired.
    NB: Tunachukua rekodi mpya zaidi (ikiwa zipo nyingi) na kulinganisha code kwa usalama.
    """
    try:
        record = (
            db.query(PasswordResetCode)
            .filter(PasswordResetCode.identifier == payload.identifier)
            .order_by(PasswordResetCode.expires_at.desc())
            .first()
        )

        if not record or record.expires_at < NOW():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired verification code",
            )

        # Tunatumia compare_digest kuepuka timing leaks (ingawa DB equality tayari ilitosha kama unge-filter_by code)
        if not secrets.compare_digest(str(record.code), str(payload.code)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired verification code",
            )

        return MessageResponse(message="Verification code is valid")

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to verify code") from exc


@router.post(
    "/reset-password",
    summary="Reset password after verification",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
def reset_password(payload: ResetPassword, db: Session = Depends(get_db)):
    """
    Reset the user's password if the verification code is valid and unexpired.
    Pia tunaondoa codes zote za identifier ili kuepuka matumizi tena (one-time).
    """
    try:
        # 1) Angalia rekodi mpya zaidi ya identifier huyu
        record = (
            db.query(PasswordResetCode)
            .filter(PasswordResetCode.identifier == payload.identifier)
            .order_by(PasswordResetCode.expires_at.desc())
            .first()
        )

        if not record or record.expires_at < NOW():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired verification code",
            )

        if not secrets.compare_digest(str(record.code), str(payload.code)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired verification code",
            )

        # 2) Thibitisha mtumiaji kisha weka password mpya
        user = get_user_by_identifier(db, payload.identifier)
        # Anti-enumeration: badala ya 404, tutarudisha ujumbe wa mafanikio hata kama user hayupo.
        if user:
            user.password = get_password_hash(payload.new_password)

        # 3) One-time: futa CODES zote za identifier huyu (ikiwemo zilizokwisha)
        db.query(PasswordResetCode).filter(
            PasswordResetCode.identifier == payload.identifier
        ).delete(synchronize_session=False)

        db.commit()
        return MessageResponse(message="Password has been reset successfully")

    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to reset password") from exc

