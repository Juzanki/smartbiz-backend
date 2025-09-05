from __future__ import annotations
﻿from backend.schemas.user import UserOut
from backend.models.product import Product
from sqlalchemy.orm import Session

# --- Simulate AI Negotiation ---


def negotiate_price(db: Session, product_id: int, user_message: str) -> dict:
    product = db.query(Product).filter(Product["id"] == product_id).first()
    if not product:
        return {"error": "Product not found"}

    original_price = product.price
    discount = 0.05 * original_price  # 5% discount simulation
    proposed_reply = f'Thank you for your interest. We can offer this for {original_price - discount:.2f} TZS today.'

    return {
        "product_name": product["name"]
        "original_price": original_price,
        "suggested_discount": discount,
        "proposed_reply": proposed_reply
    }


