from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models import Inventory
from backend.models import InventoryLog
from datetime import datetime
from typing import List

# --- Add to Inventory ---


def add_stock(db: Session, product_id: int, quantity: int) -> Inventory:
    inv = db.query(Inventory).filter(
        Inventory.product_id == product_id).first()
    if inv:
        inv.quantity += quantity
    else:
        inv = Inventory(product_id=product_id, quantity=quantity)
        db.add(inv)
    db.commit()
    db.refresh(inv)

    # Log stock addition
    log = InventoryLog(
        product_id=product_id,
        quantity=quantity,
        action='add',
        timestamp=datetime.utcnow())
    db.add(log)
    db.commit()

    return inv

# --- Remove from Inventory ---


def remove_stock(db: Session, product_id: int, quantity: int) -> Inventory:
    inv = db.query(Inventory).filter(
        Inventory.product_id == product_id).first()
    if inv and inv.quantity >= quantity:
        inv.quantity -= quantity
        db.commit()
        db.refresh(inv)

        # Log stock removal
        log = InventoryLog(
            product_id=product_id,
            quantity=quantity,
            action='remove',
            timestamp=datetime.utcnow())
        db.add(log)
        db.commit()

        return inv
    return None

# --- Get Inventory ---


def get_inventory(db: Session) -> List[Inventory]:
    return db.query(Inventory).all()


