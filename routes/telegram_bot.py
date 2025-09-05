# backend/routes/telegram.py
# -*- coding: utf-8 -*-
"""
Telegram Bot Webhook (mobile-first, international-ready)

Highlights
- Verifies Telegram webhook via 'X-Telegram-Bot-Api-Secret-Token' (set TELEGRAM_WEBHOOK_SECRET env var)
- Robust update parsing (message, edited_message, callback_query, channel_post)
- MarkdownV2-safe escaping to prevent formatting errors
- Lightweight command handling: /start, /help, /id, /stop
- Friendly bilingual responses: recognizes 'asante' (Swahili) but keeps code & defaults in English
- Soft idempotency/duplicate suppression using an in-memory LRU of update_ids
- Graceful failures return 200 OK to avoid Telegram retry storms
- Optional integrations: send typing action, answer callback queries, if your utils expose them

Notes:
- Expects async utils in backend.utils.telegram_bot:
    - send_telegram_message(chat_id: str, message: str, **kwargs)
    - (optional) send_telegram_action(chat_id: str, action: str)
    - (optional) answer_callback_query(callback_query_id: str, text: str | None = None)
- All timestamps & logic are minimal and mobile friendly.
"""
from __future__ import annotations
import os
import logging
import re
from collections import deque
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Request, HTTPException, Header, status

# Your existing utils (the optional ones are used if present)
from backend.utils.telegram_bot import send_telegram_message  # type: ignore

try:
    from backend.utils.telegram_bot import send_telegram_action  # type: ignore
except Exception:  # pragma: no cover
    send_telegram_action = None  # type: ignore

try:
    from backend.utils.telegram_bot import answer_callback_query  # type: ignore
except Exception:  # pragma: no cover
    answer_callback_query = None  # type: ignore

router = APIRouter(prefix="/telegram", tags=["Telegram Bot"])

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---- Configuration ----
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")  # Set this to enable request verification
PARSE_MODE = "MarkdownV2"  # Safer, modern Telegram markdown

# ---- Soft de-duplication of update_id (in-memory, best effort) ----
_SEEN_UPDATES: deque[int] = deque(maxlen=2048)
_SEEN_SET: set[int] = set()

def _dedupe(update_id: Optional[int]) -> bool:
    """Return True if already seen."""
    if update_id is None:
        return False
    if update_id in _SEEN_SET:
        return True
    _SEEN_SET.add(update_id)
    _SEEN_UPDATES.append(update_id)
    # keep sets in sync as deque evicts
    if len(_SEEN_UPDATES) == _SEEN_UPDATES.maxlen:
        while len(_SEEN_SET) > len(_SEEN_UPDATES):
            # remove arbitrary extra ids (rare)
            _SEEN_SET.pop()
    return False

# ---- MarkdownV2 escaping ----
_MD2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
_MD2_RE = re.compile(r"([_*\[\]\(\)~`>#+\-=|{}\.!])")

def escape_md2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    if not text:
        return text
    return _MD2_RE.sub(r"\\\1", text)

# ---- Update parsing ----
def extract_chat_and_text(update: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (chat_id, text_or_data, callback_query_id).
    Supports: message, edited_message, channel_post, callback_query.
    """
    # callback_query (inline keyboard)
    if "callback_query" in update:
        cb = update["callback_query"] or {}
        cq_id = cb.get("id")
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id")) if chat.get("id") is not None else None
        data = cb.get("data") or None
        return chat_id, data, cq_id

    # message / edited_message
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        if key in update:
            m = update[key] or {}
            chat = m.get("chat") or {}
            chat_id = str(chat.get("id")) if chat.get("id") is not None else None
            text = m.get("text") or m.get("caption") or None
            return chat_id, text, None

    return None, None, None

def is_command(text: str) -> bool:
    return bool(text and text.strip().startswith("/"))

async def maybe_typing(chat_id: Optional[str]) -> None:
    if chat_id and send_telegram_action:
        try:
            await send_telegram_action(chat_id=chat_id, action="typing")
        except Exception:
            pass  # non-fatal

async def safe_send_message(chat_id: str, message: str) -> None:
    """
    Sends a message using MarkdownV2 when possible; falls back to plain text if util doesn't support kwargs.
    """
    msg = escape_md2(message)
    try:
        await send_telegram_message(chat_id=chat_id, message=msg, parse_mode=PARSE_MODE, disable_web_page_preview=True)
    except TypeError:
        # Utils may not accept parse_mode; retry without kwargs
        await send_telegram_message(chat_id=chat_id, message=msg)
    except Exception as e:
        logger.warning("send_telegram_message failed: %s", e)

# ---- Command handling ----
def build_help() -> str:
    return (
        "*SmartBiz Bot Commands*\n"
        "/start — Start the bot\n"
        "/help — Show help\n"
        "/id — Show your chat ID\n"
        "/stop — Stop notifications\n\n"
        "_Tip: You can ask about billing, orders, or type 'asante' 😊_"
    )

def build_welcome() -> str:
    return (
        "👋 *Welcome to SmartBiz Bot!*\n\n"
        "Ask anything about your business and we’ll help.\n"
        "Use /help to see what I can do."
    )

@router.post("/webhook", summary="Telegram Bot Webhook", status_code=status.HTTP_200_OK)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None, convert_underscores=False),
):
    """
    Handles incoming Telegram updates and replies with a compact, mobile-friendly message.
    Returns 200 OK for most cases to prevent retry storms; only rejects invalid secrets.
    """
    # 1) Verify secret if configured
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        # Reject unknown sources explicitly
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid webhook secret")

    # 2) Parse body
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        # Bad JSON: ignore with 200 to avoid retries
        return {"status": "ignored", "reason": "invalid-json"}

    update_id = data.get("update_id")
    if _dedupe(update_id):
        return {"status": "ok", "deduped": True}

    chat_id, text_or_data, callback_query_id = extract_chat_and_text(data)
    if not chat_id:
        # Non-message updates we don't handle (e.g., my_chat_member): acknowledge
        return {"status": "ok", "ignored": True}

    # 3) Decide on reply
    reply: Optional[str] = None
    text = (text_or_data or "").strip()

    # Basic bilingual nicety
    lower = text.lower()

    try:
        await maybe_typing(chat_id)

        if callback_query_id and answer_callback_query:
            # Acknowledge button taps fast (non-blocking UX)
            try:
                await answer_callback_query(callback_query_id, text=None)
            except Exception:
                pass

        if not text:
            reply = "I received your update."
        elif is_command(text):
            cmd = lower.split()[0]
            if cmd == "/start":
                reply = build_welcome()
            elif cmd == "/help":
                reply = build_help()
            elif cmd == "/id":
                reply = f"Your chat ID: `{chat_id}`"
            elif cmd == "/stop":
                reply = "Okay, you will no longer receive notifications here."
            else:
                reply = "Unknown command. Use /help."
        elif "asante" in lower:
            # Friendly Swahili response while keeping codebase English
            reply = "🙏 Karibu sana! If you need anything else, just ask."
        else:
            # Echo-style acknowledgement (safe, escaped)
            reply = f"📩 You said:\n\n{text}\n\n_We’ll get back to you shortly._"

        if reply:
            await safe_send_message(chat_id, reply)

        return {"status": "ok", "chat_id": chat_id}

    except Exception as e:
        # Log, but still 200 OK to avoid Telegram retries
        logger.exception("telegram_webhook error: %s", e)
        return {"status": "ok", "error": "handled"}
