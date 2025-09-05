from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.recharge_transaction import RechargeTransaction
from backend.models.balance import Balance
from backend.schemas.recharge_schemas import RechargeCreate
from datetime import datetime

def create_recharge(db: Session, user_id: int, data: RechargeCreate):
    new_txn = RechargeTransaction(
        user_id=user_id,
        amount=data.amount,
        method=data.method,
        reference=data.reference,
        status="pending"
    )
    db.add(new_txn)
    db.commit()
    db.refresh(new_txn)
    return new_txn

def complete_recharge(db: Session, reference: str):
    txn = db.query(RechargeTransaction).filter(RechargeTransaction.reference == reference).first()
    if txn and txn.status == "pending":
        txn.status = "success"
        txn.created_at = datetime.utcnow()
        db.commit()
        db.refresh(txn)

        # Update balance
        balance = db.query(Balance).filter(Balance.user_id == txn.user_id).first()
        if balance:
            balance.amount += txn.amount
        else:
            balance = Balance(user_id=txn.user_id, amount=txn.amount)
            db.add(balance)
        db.commit()
        return txn
    return None

