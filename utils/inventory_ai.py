from datetime import datetime, timedelta

def check_refill_needed(product, threshold=5):
    """
    Check if stock is below the threshold and recommend refill.
    """
    if product.stock_quantity <= threshold:
        return f"⚠️ Stock ya '{product.name}' imebaki kidogo. Ongeza sasa?"
    return None
