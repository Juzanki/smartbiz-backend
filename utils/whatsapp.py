import httpx
import os

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")

def send_whatsapp_message(phone: str, message: str):
    """
    Sends a WhatsApp message using Meta Cloud API (or mock).
    """
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        # MOCK fallback
        print(f"[MOCK] Sending WhatsApp to {phone}: {message}")
        return {"status": "mock", "to": phone, "message": message}

    url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {
            "body": message
        }
    }

    res = httpx.post(url, headers=headers, json=payload)
    return res.json()
