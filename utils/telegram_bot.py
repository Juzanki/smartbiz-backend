from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.orm import Session
from backend.db import get_db
from backend.models.message import MessageLog
import os
import httpx

router = APIRouter()  # Declare router

# ==================== TELEGRAM CONFIG ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ==================== HELPER: Send Message ====================
async def send_telegram_message(chat_id: str, message: str) -> dict:
    url = f"{BASE_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload)
        return response.json()

# (Optional) HELPER: Send Message with Buttons
async def send_message_with_buttons(chat_id: str, message: str, buttons: list) -> dict:
    url = f"{BASE_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "reply_markup": {
            "inline_keyboard": buttons
        }
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload)
        return response.json()

# ==================== TELEGRAM WEBHOOK ====================
@router.post("/telegram/webhook", summary="ðŸŽ¯ Telegram Bot Webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    data = await request.json()

    try:
        message = data.get("message", {})
        chat = message.get("chat", {})
        chat_id = str(chat.get("id"))
        sender_name = chat.get("first_name", "Unknown")
        text = message.get("text", "")

        # Save to database
        log = MessageLog(
            chat_id=chat_id,
            sender=sender_name,
            message=text
        )
        db.add(log)
        db.commit()

        # Respond based on message
        if text.lower() == "/start":
            reply = "ðŸ‘‹ Karibu kwenye SmartBiz Bot! Andika ujumbe wako..."
        else:
            reply = f"Umesema: _{text}_\nTutakujibu haraka!"

        await send_telegram_message(chat_id=chat_id, message=reply)
        return {"ok": True}

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {str(e)}")



