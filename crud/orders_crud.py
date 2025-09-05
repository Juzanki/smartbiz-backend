from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from typing import List
from backend.models import SmartOrder
from backend.schemas.orders import SmartOrderOut

# --- Get All Orders ---


def get_all_orders(db: Session) -> List[SmartOrder]:
    return db.query(SmartOrder).order_by(SmartOrder["created_at"]desc()).all()

# --- Get Orders by Status ---


def get_orders_by_status(db: Session, status: str) -> List[SmartOrder]:
    return db.query(SmartOrder).filter(SmartOrder["status"] == status).order_by(SmartOrder["created_at"]desc()).all()


