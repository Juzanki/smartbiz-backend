from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.withdraw import WithdrawRequest
from backend.schemas.withdraw_schemas import WithdrawRequestCreate
from datetime import datetime

def create_withdraw_request(db: Session, user_id: int, data: WithdrawRequestCreate):
    request = WithdrawRequest(user_id=user_id, amount=data.amount)
    db.add(request)
    db.commit()
    db.refresh(request)
    return request

def get_pending_withdrawals(db: Session):
    return db.query(WithdrawRequest).filter(WithdrawRequest.status == "pending").all()

def approve_withdrawal(db: Session, request_id: int):
    request = db.query(WithdrawRequest).filter(WithdrawRequest.id == request_id).first()
    if request:
        request.status = "approved"
        request.approved_at = datetime.utcnow()
        db.commit()
        db.refresh(request)
    return request

def reject_withdrawal(db: Session, request_id: int):
    request = db.query(WithdrawRequest).filter(WithdrawRequest.id == request_id).first()
    if request:
        request.status = "rejected"
        db.commit()
        db.refresh(request)
    return request

