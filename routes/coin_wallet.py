from __future__ import annotations
# backend/routes/coin_wallet.py
import os
import hashlib
from decimal import Decimal, ROUND_DOWN, InvalidOperation, getcontext
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from contextlib import suppress

from fastapi import (
    APIRouter, Depends, HTTPException, status, Header, Response, Query, Path
)
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func

from backend.db import get_db
from backend import models

# -------- Auth & RBAC (best effort) --------
try:
    from backend.auth import get_current_user
except Exception:
    # Fallback: kumbuka kubadilisha hadi auth yako halisi
    def get_current_user():
        raise HTTPException(status_code=401, detail="Auth not configured")

try:
    from backend.dependencies import check_admin
except Exception:
    def check_admin(user=Depends(get_current_user)):
        role = getattr(user, "role", "user")
        if role not in {"admin", "owner"}:
            raise HTTPException(status_code=403, detail="Admin only")
        return user

# -------- Schemas (fallbacks kama hazipo) --------
try:
    from backend.schemas import coin_wallet as coin_wallet_s
    CoinWalletCreate = coin_wallet_s.CoinWalletCreate
    CoinWalletUpdate = coin_wallet_s.CoinWalletUpdate
    CoinWalletResponse = coin_wallet_s.CoinWalletResponse
    DepositRequest = coin_wallet_s.DepositRequest  # amount, currency?, reference?
    WithdrawRequest = coin_wallet_s.WithdrawRequest
    TxnOut = coin_wallet_s.CoinWalletTxnOut
except Exception:
    from pydantic import BaseModel, Field

    class CoinWalletCreate(BaseModel):
        # body haitatumika kwa /me; user_id inawekwa toka token
        user_id: Optional[int] = None
        balance: Optional[Decimal] = Field(default=Decimal("0"))

    class CoinWalletUpdate(BaseModel):
        balance: Decimal

    class CoinWalletResponse(BaseModel):
        id: int
        user_id: int
        balance: Decimal
        currency: Optional[str] = "TZS"
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None

    class DepositRequest(BaseModel):
        amount: Decimal = Field(..., gt=0)
        currency: Optional[str] = None
        reference: Optional[str] = Field(None, max_length=80)

    class WithdrawRequest(BaseModel):
        amount: Decimal = Field(..., gt=0)
        currency: Optional[str] = None
        reference: Optional[str] = Field(None, max_length=80)

    class TxnOut(BaseModel):
        id: int
        wallet_id: int
        type: str
        amount: Decimal
        balance_after: Decimal
        currency: Optional[str] = None
        reference: Optional[str] = None
        idempotency_key: Optional[str] = None
        created_at: Optional[datetime] = None

# -------- Optional ledger model --------
LedgerModel = None
with suppress(Exception):
    from backend.models.coin_wallet_txn import CoinWalletTxn as LedgerModel  # optional

router = APIRouter(prefix="/wallets", tags=["Coin Wallet"])

# -------- Money settings --------
getcontext().prec = 28  # high precision
WALLET_DECIMALS = int(os.getenv("WALLET_DECIMALS", "2"))  # 2 for fiat; 8 for crypto
WALLET_CURRENCY = os.getenv("WALLET_DEFAULT_CURRENCY", "TZS")
MIN_AMOUNT = Decimal(os.getenv("WALLET_MIN_AMOUNT", "0.01"))

def _q(amount: Decimal) -> Decimal:
    """Quantize amount to wallet decimals."""
    return amount.quantize(Decimal(10) ** -WALLET_DECIMALS, rounding=ROUND_DOWN)

def _parse_amount(v: Decimal) -> Decimal:
    try:
        amt = Decimal(v)
    except (InvalidOperation, TypeError):
        raise HTTPException(status_code=422, detail="Invalid amount")
    amt = _q(amt)
    if amt < MIN_AMOUNT:
        raise HTTPException(status_code=422, detail=f"Amount must be â‰¥ {MIN_AMOUNT}")
    return amt

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _etag_wallet(w) -> str:
    base = f"{getattr(w, 'id', '')}-{getattr(w, 'balance', '')}-{getattr(w, 'updated_at', '')}"
    return 'W/"' + hashlib.sha256(str(base).encode("utf-8")).hexdigest()[:16] + '"'

# ============================== CREATE (me) ==============================
@router.post(
    "/me",
    response_model=CoinWalletResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Unda wallet kwa current user (idempotent via UNIQUE constraint)"
)
def create_my_wallet(
    payload: CoinWalletCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_id = getattr(current_user, "id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Heshimu default 0 kama balance ya mwanzo
    init_balance = _q(payload.balance if getattr(payload, "balance", None) else Decimal("0"))

    # Idempotent via unique(user_id)
    try:
        w = models.CoinWallet(user_id=user_id, balance=init_balance, currency=WALLET_CURRENCY)
        if hasattr(w, "created_at") and not getattr(w, "created_at", None):
            w.created_at = _utcnow()
        if hasattr(w, "updated_at"):
            w.updated_at = _utcnow()
        db.add(w)
        db.commit()
        db.refresh(w)
    except IntegrityError:
        db.rollback()
        # Ikiwepo tayari, rudisha hiyo
        w = db.query(models.CoinWallet).filter(models.CoinWallet.user_id == user_id).first()
        if not w:
            raise HTTPException(status_code=500, detail="Wallet create race detected")

    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = _etag_wallet(w)

    # Pydantic v1/v2 friendly
    if hasattr(CoinWalletResponse, "model_validate"):
        return CoinWalletResponse.model_validate(w, from_attributes=True)  # type: ignore
    return CoinWalletResponse.model_validate(w)  # type: ignore

# ============================== GET (me) ==============================
@router.get(
    "/me",
    response_model=CoinWalletResponse,
    summary="Pata wallet ya current user"
)
def get_my_wallet(
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    w = db.query(models.CoinWallet).filter(models.CoinWallet.user_id == current_user.id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = _etag_wallet(w)
    return CoinWalletResponse.model_validate(w, from_attributes=True) if hasattr(CoinWalletResponse, "model_validate") else CoinWalletResponse.model_validate(w)  # type: ignore

# ============================== ADMIN: GET by user_id ==============================
@router.get(
    "/{user_id}",
    response_model=CoinWalletResponse,
    summary="(Admin) Pata wallet ya mtumiaji kwa user_id",
    dependencies=[Depends(check_admin)]
)
def admin_get_wallet(user_id: int, db: Session = Depends(get_db)):
    w = db.query(models.CoinWallet).filter(models.CoinWallet.user_id == user_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return CoinWalletResponse.model_validate(w, from_attributes=True) if hasattr(CoinWalletResponse, "model_validate") else CoinWalletResponse.model_validate(w)  # type: ignore

# ============================== PUT (admin only) ==============================
@router.put(
    "/{user_id}",
    response_model=CoinWalletResponse,
    summary="(Admin) weka balance moja kwa moja",
    dependencies=[Depends(check_admin)]
)
def admin_update_wallet(
    user_id: int,
    wallet_data: CoinWalletUpdate,
    db: Session = Depends(get_db)
):
    w = db.query(models.CoinWallet).filter(models.CoinWallet.user_id == user_id).with_for_update(of=models.CoinWallet).first()
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")

    w.balance = _q(wallet_data.balance)
    if hasattr(w, "updated_at"):
        w.updated_at = _utcnow()
    db.commit()
    db.refresh(w)
    return CoinWalletResponse.model_validate(w, from_attributes=True) if hasattr(CoinWalletResponse, "model_validate") else CoinWalletResponse.model_validate(w)  # type: ignore

# ============================== DEPOSIT (me) ==============================
@router.post(
    "/me/deposit",
    response_model=CoinWalletResponse,
    status_code=status.HTTP_200_OK,
    summary="Weka fedha kwenye wallet (idempotent + ledger ikiwa ipo)"
)
def deposit_my_wallet(
    payload: DepositRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    amount = _parse_amount(payload.amount)

    # Idempotency check via ledger table if available
    if LedgerModel and idempotency_key:
        dup = db.query(LedgerModel).filter(
            LedgerModel.wallet_id == models.CoinWallet.id,  # logical, refined below after we get wallet
        )

    # Lock wallet row
    w = db.query(models.CoinWallet).filter(models.CoinWallet.user_id == current_user.id)\
        .with_for_update(of=models.CoinWallet).first()
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")

    # (Optional) guard currency
    currency = getattr(w, "currency", WALLET_CURRENCY)
    if payload.currency and payload.currency != currency:
        raise HTTPException(status_code=422, detail=f"Currency mismatch. Wallet is {currency}")

    # Idempotency via ledger: check after we know wallet id
    if LedgerModel and idempotency_key:
        dup = db.query(LedgerModel).filter(
            LedgerModel.wallet_id == w.id,
            LedgerModel.idempotency_key == idempotency_key,
        ).first()
        if dup:
            # Already applied: return current wallet state
            response.headers["Cache-Control"] = "no-store"
            response.headers["ETag"] = _etag_wallet(w)
            return CoinWalletResponse.model_validate(w, from_attributes=True) if hasattr(CoinWalletResponse, "model_validate") else CoinWalletResponse.model_validate(w)  # type: ignore

    # Apply deposit
    w.balance = _q(Decimal(w.balance) + amount)
    if hasattr(w, "updated_at"):
        w.updated_at = _utcnow()
    db.flush()  # get wallet id

    # Ledger write (if model exists)
    if LedgerModel:
        txn = LedgerModel(
            wallet_id=w.id,
            type="deposit",
            amount=amount,
            balance_after=w.balance,
            currency=currency,
            reference=getattr(payload, "reference", None),
            idempotency_key=idempotency_key,
            created_at=_utcnow(),
        )
        db.add(txn)

    db.commit()
    db.refresh(w)

    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = _etag_wallet(w)
    return CoinWalletResponse.model_validate(w, from_attributes=True) if hasattr(CoinWalletResponse, "model_validate") else CoinWalletResponse.model_validate(w)  # type: ignore

# ============================== WITHDRAW (me) ==============================
@router.post(
    "/me/withdraw",
    response_model=CoinWalletResponse,
    summary="Toa fedha (salio lazima litoche; idempotent + ledger ikiwa ipo)"
)
def withdraw_my_wallet(
    payload: WithdrawRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    amount = _parse_amount(payload.amount)

    # Lock wallet
    w = db.query(models.CoinWallet).filter(models.CoinWallet.user_id == current_user.id)\
        .with_for_update(of=models.CoinWallet).first()
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")

    currency = getattr(w, "currency", WALLET_CURRENCY)
    if payload.currency and payload.currency != currency:
        raise HTTPException(status_code=422, detail=f"Currency mismatch. Wallet is {currency}")

    # Idempotency via ledger if available
    if LedgerModel and idempotency_key:
        dup = db.query(LedgerModel).filter(
            LedgerModel.wallet_id == w.id,
            LedgerModel.idempotency_key == idempotency_key,
            LedgerModel.type == "withdraw",
        ).first()
        if dup:
            response.headers["Cache-Control"] = "no-store"
            response.headers["ETag"] = _etag_wallet(w)
            return CoinWalletResponse.model_validate(w, from_attributes=True) if hasattr(CoinWalletResponse, "model_validate") else CoinWalletResponse.model_validate(w)  # type: ignore

    # Check balance
    new_balance = _q(Decimal(w.balance) - amount)
    if new_balance < Decimal("0"):
        raise HTTPException(status_code=409, detail="Insufficient balance")
    w.balance = new_balance
    if hasattr(w, "updated_at"):
        w.updated_at = _utcnow()
    db.flush()

    # Ledger
    if LedgerModel:
        txn = LedgerModel(
            wallet_id=w.id,
            type="withdraw",
            amount=amount,
            balance_after=w.balance,
            currency=currency,
            reference=getattr(payload, "reference", None),
            idempotency_key=idempotency_key,
            created_at=_utcnow(),
        )
        db.add(txn)

    db.commit()
    db.refresh(w)

    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = _etag_wallet(w)
    return CoinWalletResponse.model_validate(w, from_attributes=True) if hasattr(CoinWalletResponse, "model_validate") else CoinWalletResponse.model_validate(w)  # type: ignore

# ============================== TRANSACTIONS (me) ==============================
@router.get(
    "/me/transactions",
    response_model=List[TxnOut],
    summary="Orodha ya miamala ya wallet (ikiwa ledger ipo)",
)
def list_my_transactions(
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    type_eq: Optional[str] = Query(None, description="deposit/withdraw"),
    order: str = Query("desc", regex="^(asc|desc)$"),
):
    if not LedgerModel:
        raise HTTPException(status_code=501, detail="Ledger not available")

    w = db.query(models.CoinWallet).filter(models.CoinWallet.user_id == current_user.id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")

    q = db.query(LedgerModel).filter(LedgerModel.wallet_id == w.id)
    if type_eq:
        q = q.filter(LedgerModel.type == type_eq)

    q = q.order_by(LedgerModel.id.asc() if order == "asc" else LedgerModel.id.desc())
    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    # Pydantic v1/v2
    out: List[Any] = []
    for r in rows:
        if hasattr(TxnOut, "model_validate"):
            out.append(TxnOut.model_validate(r, from_attributes=True))  # type: ignore
        else:
            out.append(TxnOut.model_validate(r))  # type: ignore
    return out

