from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.wallet import Wallet, WalletTransaction
from backend.schemas.wallet import WalletTransactionCreate
from datetime import datetime

# === WALLET ===
def get_or_create_wallet(db: Session, user_id: int):
    wallet = db.query(Wallet).filter(Wallet.user_id == user_id).first()
    if not wallet:
        wallet = Wallet(user_id=user_id)
        db.add(wallet)
        db.commit()
        db.refresh(wallet)
    return wallet

# === TRANSACTIONS ===
def create_transaction(db: Session, tx: WalletTransactionCreate, status: str = "success"):
    transaction = WalletTransaction(
        wallet_id=tx.wallet_id,
        type=tx.type,
        amount=tx.amount,
        currency=tx.currency,
        description=tx.description,
        status=status,
        created_at=datetime.utcnow()
    )
    db.add(transaction)
    db.commit()
    db.refresh(transaction)
    return transaction

def get_wallet_transactions(db: Session, wallet_id: int):
    return db.query(WalletTransaction).filter(WalletTransaction.wallet_id == wallet_id).order_by(WalletTransaction.created_at.desc()).all()

