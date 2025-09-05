from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.smart_coin_transaction import SmartCoinTransaction
from datetime import datetime
from typing import List

# --- Add Coins ---


def add_coins(db: Session, user_id: int, amount: int,
              reason: str = "Top-up") -> SmartCoinTransaction:
    entry = SmartCoinTransaction(
        user_id=user_id,
        amount=amount,
        reason=reason,
        direction='in',
        timestamp=datetime.utcnow()
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry

# --- Deduct Coins ---


def deduct_coins(db: Session, user_id: int, amount: int,
                 reason: str = "Spent") -> SmartCoinTransaction:
    entry = SmartCoinTransaction(
        user_id=user_id,
        amount=amount,
        reason=reason,
        direction='out',
        timestamp=datetime.utcnow()
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry

# --- Get Coin Balance ---


def get_coin_balance(db: Session, user_id: int) -> int:
    transactions = db.query(SmartCoinTransaction).filter(
        SmartCoinTransaction.user_id == user_id).all()
    return sum([t.amount if t.direction == 'in' else -
               t.amount for t in transactions])


