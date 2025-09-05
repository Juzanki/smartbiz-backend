from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.schemas.wallet import WalletTransactionCreate, WalletTransactionOut, WalletOut
from backend.crud import wallet_crud

router = APIRouter(
    prefix="/wallet",
    tags=["Wallet"]
)

@router.get("/", response_model=WalletOut)
def get_my_wallet(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return wallet_crud.get_or_create_wallet(db, user_id=current_user.id)

@router.get("/transactions", response_model=list[WalletTransactionOut])
def get_my_transactions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    wallet = wallet_crud.get_or_create_wallet(db, user_id=current_user.id)
    return wallet_crud.get_wallet_transactions(db, wallet_id=wallet.id)

@router.post("/deposit", response_model=WalletTransactionOut)
def deposit_funds(
    tx: WalletTransactionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    wallet = wallet_crud.get_or_create_wallet(db, current_user.id)
    if wallet.id != tx.wallet_id:
        raise HTTPException(403, detail="Invalid wallet")

    wallet.balance += tx.amount
    db.commit()
    return wallet_crud.create_transaction(db, tx)

@router.post("/withdraw", response_model=WalletTransactionOut)
def withdraw_funds(
    tx: WalletTransactionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    wallet = wallet_crud.get_or_create_wallet(db, current_user.id)
    if wallet.id != tx.wallet_id:
        raise HTTPException(403, detail="Invalid wallet")
    if wallet.balance < tx.amount:
        raise HTTPException(400, detail="Insufficient funds")

    wallet.balance -= tx.amount
    db.commit()
    return wallet_crud.create_transaction(db, tx)

@router.post("/convert", response_model=WalletTransactionOut)
def convert_to_smartcoin(
    tx: WalletTransactionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    wallet = wallet_crud.get_or_create_wallet(db, current_user.id)
    if wallet.id != tx.wallet_id:
        raise HTTPException(403, detail="Invalid wallet")
    if tx.currency == "TZS":
        # convert TZS to SmartCoin
        if wallet.balance < tx.amount:
            raise HTTPException(400, detail="Insufficient funds")
        wallet.balance -= tx.amount
        wallet.smartcoin += tx.amount
    elif tx.currency.lower() == "smartcoin":
        if wallet.smartcoin < tx.amount:
            raise HTTPException(400, detail="Insufficient SmartCoin")
        wallet.smartcoin -= tx.amount
        wallet.balance += tx.amount
    else:
        raise HTTPException(400, detail="Invalid currency")

    db.commit()
    return wallet_crud.create_transaction(db, tx)
