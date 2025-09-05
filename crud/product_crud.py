from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from typing import List
from backend.models.product import Product
from backend.schemas.product import ProductOut

# --- Create Product ---


def create_product(db: Session, data: dict) -> Product:
    product = Product(**data)
    db.add(product)
    db.commit()
    db.refresh(product)
    return product

# --- Get All Products ---


def get_all_products(db: Session) -> List[Product]:
    return db.query(Product).order_by(Product["created_at"]desc()).all()

# --- Get Product By ID ---


def get_product_by_id(db: Session, product_id: int) -> Product:
    return db.query(Product).filter(Product["id"] == product_id).first()

# --- Update Product ---


def update_product(db: Session, product_id: int, updates: dict) -> Product:
    product = db.query(Product).filter(Product["id"] == product_id).first()
    if product:
        for field, value in updates.items():
            setattr(product, field, value)
        db.commit()
        db.refresh(product)
    return product

# --- Delete Product ---


def delete_product(db: Session, product_id: int) -> bool:
    product = db.query(Product).filter(Product["id"] == product_id).first()
    if product:
        db.delete(product)
        db.commit()
        return True
    return False


