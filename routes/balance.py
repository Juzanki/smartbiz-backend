from __future__ import annotations
# backend/routes/wallet.py
import os
import time
import logging
from typing import Optional, Dict

from fastapi import (
    APIRouter, Depends, HTTPException, status, Header, Response, Query
)
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.dependencies import get_current_user, get_admin_user
from backend.schemas import balance_schemas
from backend.crud import balance_crud
from backend.models.user import User as UserModel

logger = logging.getLogger("smartbiz.wallet")

router = APIRouter(tags=["Wallet / Balance"])

# --------------------- Config & Guards --------------------- #
MIN_WITHDRAW = float(os.getenv("MIN_WITHDRAW_AMOUNT", "1.0"))          # USD/your currency
MAX_WITHDRAW = float(os.getenv("MAX_WITHDRAW_AMOUNT", "1000000.0"))    # kikomo cha juu
WITHDRAW_RATE_MAX_PER_MIN = int(os.getenv("WITHDRAW_RATE_PER_MIN", "5"))

# in-memory sliding window rate bucket per user
_RATE: Dict[int, list[float]] = {}
_IDEMP: Dict[tuple[int, str], float] = {}  # (user_id, key) -> ts
_IDEMP_TTL = 10 * 60  # dk 10

def _rate_ok(user_id: int) -> int:
    """Rudisha iliyosalia baada ya kuingia kwenye dirisha la dakika."""
    now = time.time()
    q = _RATE.setdefault(user_id, [])
    while q and (now - q[0]) > 60.0:
        q.pop(0)
    if len(q) >= WITHDRAW_RATE_MAX_PER_MIN:
        return 0
    q.append(now)
    return max(0, WITHDRAW_RATE_MAX_PER_MIN - len(q))

def _check_idempotency(user_id: int, key: Optional[str]) -> None:
    """Zuia marudio ya haraka kwa idempotency key (hiari)."""
    if not key:
        return
    now = time.time()
    # purge old
    stale = []
    for (uid, k), ts in _IDEMP.items():
        if now - ts > _IDEMP_TTL:
            stale.append((uid, k))
    for s in stale:
        _IDEMP.pop(s, None)

    token = (user_id, key.strip())
    if token in _IDEMP:
        raise HTTPException(status_code=409, detail="Duplicate request (Idempotency-Key)")
    _IDEMP[token] = now


# --------------------------- Endpoints --------------------------- #
@router.get(
    "/balance/me",
    response_model=balance_schemas.BalanceOut,
    summary="Pata balance ya mtumiaji aliyeingia"
)
def get_my_balance(
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    balance = balance_crud.get_user_balance(db, current_user.id)
    if not balance:
        raise HTTPException(status_code=404, detail="Balance not found")
    # Mobile caching: epuka kuhifadhi balance ya sasa
    response.headers["Cache-Control"] = "no-store"
    return balance


@router.post(
    "/withdraw",
    response_model=balance_schemas.WithdrawRequestOut,
    status_code=status.HTTP_201_CREATED,
    summary="Tuma ombi la kujitoa (withdraw)"
)
def request_withdrawal(
    data: balance_schemas.WithdrawRequestCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    # --- Rate limit ---
    remaining = _rate_ok(current_user.id)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    if remaining <= 0:
        raise HTTPException(status_code=429, detail="Too many withdrawal attempts. Try again shortly.")

    # --- Idempotency (optional but recommended by clients) ---
    _check_idempotency(current_user.id, idempotency_key)

    # --- Validate amount if present on schema ---
    amount = None
    # Pydantic v1/v2: tumia getattr bila kuvunja
    for candidate in ("amount", "value", "qty"):
        if hasattr(data, candidate):
            amount = getattr(data, candidate)
            break
    if amount is not None:
        try:
            amount = float(amount)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid amount format")

        if amount < MIN_WITHDRAW:
            raise HTTPException(status_code=422, detail=f"Minimum withdraw is {MIN_WITHDRAW}")
        if amount > MAX_WITHDRAW:
            raise HTTPException(status_code=422, detail=f"Maximum withdraw is {MAX_WITHDRAW}")

        # hakikisha balance inatosha
        try:
            bal = balance_crud.get_user_balance(db, current_user.id)
        except Exception as e:
            logger.exception("get_user_balance failed: %s", e)
            raise HTTPException(status_code=500, detail="Failed to check balance")
        if not bal:
            raise HTTPException(status_code=404, detail="Balance not found")
        # try kusoma available-like field kwa majina ya kawaida
        available = None
        for f in ("available", "available_amount", "balance", "current"):
            if hasattr(bal, f):
                available = getattr(bal, f)
                break
        if available is not None and float(available) < float(amount):
            raise HTTPException(status_code=422, detail="Insufficient balance")

    # --- Create request via CRUD ---
    try:
        wr = balance_crud.create_withdraw_request(db, current_user.id, data)
    except Exception as e:
        logger.exception("create_withdraw_request failed: %s", e)
        # on failure, free idempotency slot for next retry
        if idempotency_key:
            _IDEMP.pop((current_user.id, idempotency_key), None)
        raise HTTPException(status_code=500, detail="Failed to create withdrawal request")

    response.headers["Cache-Control"] = "no-store"
    return wr


@router.get(
    "/withdrawals/pending",
    response_model=list[balance_schemas.WithdrawRequestOut],
    summary="ðŸ” Orodha ya pending withdrawals (admin)"
)
def list_pending_withdrawals(
    db: Session = Depends(get_db),
    admin: UserModel = Depends(get_admin_user),
    limit: Optional[int] = Query(None, ge=1, le=500),
    offset: Optional[int] = Query(None, ge=0),
):
    """
    Jaribu kupitisha pagination kama CRUD yako inaikubali;
    vinginevyo rudi kwenye fallback isiyo na pagination.
    """
    try:
        if limit is not None or offset is not None:
            # tumia signature inayoegemea (limit, offset) ikiwa ipo
            return balance_crud.get_pending_withdrawals(db, limit=limit, offset=offset)  # type: ignore[arg-type]
    except TypeError:
        # CRUD haina signature ya pagination â€” endelea na fallback
        pass
    return balance_crud.get_pending_withdrawals(db)


@router.post(
    "/withdrawals/{request_id}/approve",
    response_model=balance_schemas.WithdrawRequestOut,
    summary="ðŸ” Idhinisha ombi la kujitoa"
)
def approve_withdrawal(
    request_id: int,
    db: Session = Depends(get_db),
    admin: UserModel = Depends(get_admin_user),
):
    try:
        return balance_crud.approve_withdrawal(db, request_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("approve_withdrawal failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to approve withdrawal")


@router.post(
    "/withdrawals/{request_id}/reject",
    response_model=balance_schemas.WithdrawRequestOut,
    summary="ðŸ” Kataa ombi la kujitoa"
)
def reject_withdrawal(
    request_id: int,
    db: Session = Depends(get_db),
    admin: UserModel = Depends(get_admin_user),
):
    try:
        return balance_crud.reject_withdrawal(db, request_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("reject_withdrawal failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to reject withdrawal")

