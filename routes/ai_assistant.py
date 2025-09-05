from __future__ import annotations
from backend.schemas.user import UserOut
# backend/routes/ai_assistant.py
import os
import time
import asyncio
import logging
from typing import Optional, AsyncGenerator, Dict

from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from backend.dependencies import get_current_user
from backend.models.user import User
logger = logging.getLogger("smartbiz.ai")

router = APIRouter(prefix="/ai-assistant", tags=["AI Assistant"])

# ----------------------------- Config -----------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY".lower())
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "").strip() or "gpt-3.5-turbo"
DEFAULT_TEMP = float(os.getenv("OPENAI_TEMPERATURE", "0.7"))
DEFAULT_MAXTOK = int(os.getenv("OPENAI_MAX_TOKENS", "800"))

if not OPENAI_API_KEY:
    # Tutainua 503 kwenye request; tuna-log tu mara moja hapa
    logger.warning("OPENAI_API_KEY is missing in environment.")

# Tujaribu client mpya; tukikosa, tutarudi kwenye openai.ChatCompletion
_USING_NEW_CLIENT = False
_async_client = None
try:
    from openai import AsyncOpenAI  # type: ignore
    _USING_NEW_CLIENT = True
    _async_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
except Exception:  # noqa: BLE001
    try:
        import openai  # type: ignore
        openai.api_key = OPENAI_API_KEY
        _USING_NEW_CLIENT = False
    except Exception:  # noqa: BLE001
        pass

# ----------------------- Lightweight rate limit -----------------------
# per-user sliding window (requests/minute). Mobile-first: rahisi na nyepesi.
_RATE: Dict[str, list[float]] = {}
RATE_WINDOW_SEC = 60
RATE_MAX_REQ = int(os.getenv("AI_RATE_LIMIT_PER_MIN", "20"))  # admin unaweza kubadili .env

def _rate_ok(user_key: str) -> bool:
    now = time.time()
    q = _RATE.setdefault(user_key, [])
    # safisha zamani
    while q and now - q[0] > RATE_WINDOW_SEC:
        q.pop(0)
    if len(q) >= RATE_MAX_REQ:
        return False
    q.append(now)
    return True


# ----------------------------- Models -----------------------------
class AIAssistantRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    language: str = Field("sw", description="ISO code e.g., 'sw','en','fr'")
    model: Optional[str] = Field(None, description="Override model; else uses env/default")
    temperature: Optional[float] = Field(None, ge=0, le=2)
    max_tokens: Optional[int] = Field(None, ge=32, le=4000)
    stream: bool = Field(False, description="Stream incremental tokens (mobile-friendly)")

class AIAssistantResponse(BaseModel):
    user: str
    task: str
    assistant_reply: str
    model: str
    usage: Optional[dict] = None


# ----------------------------- Prompting -----------------------------
def _system_prompt(lang: str) -> str:
    return (
        "You are SmartBiz Assistant: a concise, friendly business aide. "
        f"Respond in language '{lang}'. Prefer short paragraphs & bullet points. "
        "Avoid overly long answers unless asked. If unsure, ask a brief follow-up."
    )

def _build_messages(user_email: str, lang: str, prompt: str) -> list[dict]:
    return [
        {"role": "system", "content": _system_prompt(lang)},
        {"role": "user", "content": f"User: {user_email}\nTask: {prompt}"},
    ]


# ----------------------------- Core call -----------------------------
async def _call_openai_nonstream(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
) -> tuple[str, dict | None]:
    """
    Rudisha (text, usage) bila streaming. Inashika Responses API mpya au Chat Completions ya zamani.
    """
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="AI not configured (missing API key).")

    try:
        if _USING_NEW_CLIENT and _async_client is not None:
            # Responses API (nyepesi na thabiti)
            # Kumbuka: baadhi ya clients bado wana chat.completions; hii inajaribu responses kwanza.
            try:
                resp = await _async_client.responses.create(
                    model=model,
                    input=[{"role": "system", "content": messages[0]["content"]},
                           {"role": "user", "content": messages[1]["content"]}],
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                )
                # Muundo wa Responses: resp.output_text au itemsâ€¦
                text = getattr(resp, "output_text", None)
                if not text:
                    # fallback parse
                    text = ""
                    if getattr(resp, "output", None):
                        for item in resp.output:
                            if getattr(item, "type", "") == "message":
                                for c in getattr(item, "content", []) or []:
                                    text += getattr(c, "text", "") or ""
                usage = getattr(resp, "usage", None)
                usage_dict = usage.model_dump() if getattr(usage, "model_dump", None) else (dict(usage) if usage else None)
                return text or "", usage_dict
            except Exception:
                # Fallback to chat.completions if responses path not available
                pass

        # -------- Chat Completions fallback (legacy) --------
        import openai  # type: ignore
        # NB: Some client versions use openai.resources.chat.completions.Completions
        # The classic call below still works widely:
        resp = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = resp["choices"][0]["message"]["content"] if resp and resp.get("choices") else ""
        usage = resp.get("usage")
        return text or "", usage
    except Exception as e:
        logger.exception("OpenAI call failed: %s", e)
        # Map known errors if unataka, hapa tunaweka generic:
        raise HTTPException(status_code=502, detail=f"AI upstream error: {e}")


async def _stream_openai(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
) -> AsyncGenerator[bytes, None]:
    """
    Rudisha stream ya bytes (text/plain; charset=utf-8) â€” rahisi kwa mobile (incremental).
    Hutuma â€œdata:â€ lines (SSE-like) lakini bila header ya text/event-stream kwa urahisi wa universal clients.
    """
    if not OPENAI_API_KEY:
        yield b"data: [error] AI not configured (missing API key)\n\n"
        return

    # ---- Prefer Responses streaming if available ----
    if _USING_NEW_CLIENT and _async_client is not None:
        try:
            stream = await _async_client.responses.stream.create(
                model=model,
                input=[{"role": "system", "content": messages[0]["content"]},
                       {"role": "user", "content": messages[1]["content"]}],
                temperature=temperature,
                max_output_tokens=max_tokens,
            )
            async with stream as s:
                async for event in s:
                    # event types can be: "response.output_text.delta", "response.completed", etc.
                    t = getattr(event, "delta", None) or getattr(event, "output_text", None) or ""
                    if t:
                        yield f"data: {t}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
            return
        except Exception as e:
            logger.warning("Responses streaming failed, fallback to ChatCompletions: %s", e)

    # ---- Fallback: ChatCompletions streaming (sync -> thread) ----
    try:
        import openai  # type: ignore

        def _sync_stream():
            return openai.ChatCompletion.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )

        # Run blocking generator in a thread and forward chunks
        stream = await asyncio.to_thread(_sync_stream)
        for chunk in stream:  # type: ignore
            delta = (chunk.get("choices") or [{}])[0].get("delta", {})
            t = delta.get("content")
            if t:
                yield f"data: {t}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
    except Exception as e:
        logger.exception("OpenAI stream failed: %s", e)
        yield f"data: [error] {e}\n\n".encode("utf-8")


# ----------------------------- Endpoint -----------------------------
@router.post(
    "/ask",
    response_model=AIAssistantResponse,
    responses={
        200: {"description": "AI answer"},
        206: {"description": "Partial content (streaming)"},
        429: {"description": "Rate limit exceeded"},
        502: {"description": "Upstream AI error"},
        503: {"description": "AI not configured"},
    },
    summary="Tuma ombi kwa AI Assistant",
)
async def ask_ai(
    body: AIAssistantRequest,
    current_user = Depends(get_current_user),
):
    # ---- API key check ----
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="AI not configured (missing API key).")

    # ---- Rate limit per user ----
    key = f"{getattr(current_user, 'id', 'anon')}|{getattr(current_user, 'email', '')}"
    if not _rate_ok(key):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again shortly.")

    # ---- Sanitize/normalize ----
    model = (body.model or DEFAULT_MODEL).strip()
    temperature = float(body.temperature if body.temperature is not None else DEFAULT_TEMP)
    max_tokens = int(body.max_tokens if body.max_tokens is not None else DEFAULT_MAXTOK)

    messages = _build_messages(
        user_email=getattr(current_user, "email", "user@unknown"),
        lang=body.language,
        prompt=body.prompt,
    )

    # ---- Streaming branch ----
    if body.stream:
        gen = _stream_openai(messages, model=model, temperature=temperature, max_tokens=max_tokens)
        # Plain text w/ â€œdata: â€¦â€ lines (clients rahisi za mobile hupenda)
        return StreamingResponse(gen, media_type="text/plain; charset=utf-8", status_code=status.HTTP_206_PARTIAL_CONTENT)

    # ---- Non-streaming branch ----
    text, usage = await _call_openai_nonstream(messages, model=model, temperature=temperature, max_tokens=max_tokens)
    payload = AIAssistantResponse(
        user=getattr(current_user, "email", ""),
        task=body.prompt,
        assistant_reply=text,
        model=model,
        usage=usage or None,
    )
    return JSONResponse(payload.model_dump(), status_code=200)


