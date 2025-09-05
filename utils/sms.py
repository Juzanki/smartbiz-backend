import os

def send_sms_message(phone: str, message: str):
    """
    Sends SMS message via mock service or SMS API (Africa's Talking, Twilio).
    """
    # MOCK fallback
    print(f"[MOCK] Sending SMS to {phone}: {message}")
    return {"status": "mock", "to": phone, "message": message}

    # You can integrate real SMS gateway here later
    # e.g. Twilio or Africa's Talking
