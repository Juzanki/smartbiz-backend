from backend.models.product import Product
from random import randint

def generate_negotiation_reply(product: Product, message: str) -> dict:
    # Sample AI logic â€” real case can use NLP/LLM model
    base_discount = 0
    reply = "Thanks for your interest."

    if "discount" in message.lower():
        base_discount = round(product.price * 0.1, 2)
        reply = f"We can offer you a special discount of TZS {base_discount} on {product.name}."

    elif "too expensive" in message.lower():
        base_discount = round(product.price * 0.05, 2)
        reply = f"Sorry for that! We can lower it by TZS {base_discount} as a courtesy."

    else:
        reply += f" Please let us know what price you're expecting for {product.name}."

    return {
        "reply": reply,
        "suggested_discount": base_discount
    }

