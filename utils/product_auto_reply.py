# product_auto_reply.py

from backend.crud import get_all_products
from sqlalchemy.orm import Session

def generate_product_catalog_message(db: Session) -> str:
    products = get_all_products(db)
    if not products:
        return "Hakuna bidhaa zilizopo kwa sasa."

    message = "🛒 Orodha ya Bidhaa Zetu:\n"
    for p in products:
        message += f"\n• {p.name} - TZS {p.price:,.0f}\n  {p.description or ''}"
    return message
