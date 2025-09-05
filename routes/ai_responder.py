# -*- coding: utf-8 -*-
from __future__ import annotations
"""
AI Auto Responder for SmartBiz Assistant
- Access restricted to 'Pro' or 'Business' plans (router-level dependency).
- Supports JSON response or SSE streaming for real-time mobile-friendly UX.
"""
import os
import time
import asyncio
import logging
from typing import Optional, Dict, AsyncGenerator, List

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.auth import get_current_user
from backend.models.user import User
from backend.utils.access_control import require_plan

logger = logging.getLogger("smartbiz.airesponder")

# -------------------------- Router (plan-gated) -------------------------- #
router = APIRouter(
    prefix="/ai",
    tags=["AI Auto-Responder"],
    dependencies=[Depends(require_plan(["Pro", "Business"]))],
)

# -------------------------- OpenAI client setup -------------------------- #
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo").strip()
DEFAULT_TEMP = float(os.getenv("OPENAI_TEMPERATURE", "0.7"))
DEFAULT_MAXTOK = int(os.getenv("OPENAI_MAX_TOKENS", "800"))

_USING_ASYNC = False
_async_client = None

if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY is missing. /ai endpoints will return 503.")

try:
    from openai import AsyncOpenAI  # modern client
    if OPENAI_API_KEY:
        _async_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        _USING_ASYNC = True
except ImportError:
    try:
        import openai  # legacy sync fallback
        openai.api_key = OPENAI_API_KEY
    except ImportError:
        pass

# -------------------------- Rate limiting -------------------------- #
_RATE: Dict[str, List[float]] = {}
RATE_MAX_PER_MIN = int(os.getenv("AI_RATE_LIMIT_PER_MIN", "30"))
RATE_WINDOW = 60.0

def _rate_ok(key: str) -> bool:
    now = time.time()
    q = _RATE.setdefault(key, [])
    q[:] = [t for t in q if now - t <= RATE_WINDOW]  # purge old
    if len(q) >= RATE_MAX_PER_MIN:
        return False
    q.append(now)
    return True

# ------------------------------ Schemas ------------------------------ #
class PromptRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    language: str = Field("sw", description="Language code e.g., 'sw','en','fr'")
    model: Optional[str] = Field(None)
    temperature: Optional[float] = Field(None, ge=0, le=2)
    max_tokens: Optional[int] = Field(None, ge=32, le=4000)
    stream: bool = Field(False, description="Use SSE streaming")
    system_prompt: Optional[str] = Field(None)

class PromptResponse(BaseModel):
    user: str
    plan: str
    prompt: str
    response: str
    model: str
    usage: Optional[dict] = None

# ------------------------------ Helpers ------------------------------ #
def _clamp_params(model: Optional[str], temp: Optional[float], max_tok: Optional[int]):
    return (
        (model or DEFAULT_MODEL).strip(),
        DEFAULT_TEMP if temp is None else max(0.0, min(float(temp), 2.0)),
        DEFAULT_MAXTOK if max_tok is None else max(32, min(int(max_tok), 4000))
    )

def _messages(user: User, lang: str, sys_prompt: Optional[str], prompt: str) -> list[dict]:
    system_msg = sys_prompt or (
        f"You are SmartBiz Assistant. Respond in '{lang}', concise, helpful, "
        "with short paragraphs and bullet points where applicable."
    )
    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": f"{prompt}"},
    ]

# --------------------------- Core OpenAI calls --------------------------- #
async def _chat_once(messages: list[dict], model: str, temperature: float, max_tokens: int):
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="AI not configured (missing API key).")

    try:
        if _USING_ASYNC and _async_client:
            resp = await _async_client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content or "", getattr(resp, "usage", None)
        else:
            import openai  # type: ignore
            def _call():
                return openai.ChatCompletion.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            resp = await asyncio.to_thread(_call)
            return resp["choices"][0]["message"]["content"], resp.get("usage")
    except Exception as e:
        logger.exception("OpenAI chat error: %s", e)
        raise HTTPException(status_code=502, detail=f"Upstream AI error: {str(e)}")

async def _stream_sse(messages: list[dict], model: str, temperature: float, max_tokens: int) -> AsyncGenerator[bytes, None]:
    if not OPENAI_API_KEY:
        yield b"event: error\ndata: Missing API key\n\n"
        return

    yield f"event: meta\ndata: model={model}\n\n".encode()

    try:
        if _USING_ASYNC and _async_client:
            stream = await _async_client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )
            async for chunk in stream:
                if chunk.choices and (delta := chunk.choices[0].delta):
                    if text := getattr(delta, "content", None):
                        yield f"data: {text}\n\n".encode()
            yield b"event: done\ndata: [DONE]\n\n"
            return
    except Exception as e:
        logger.warning("Async stream failed, falling back: %s", e)

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
        stream = await asyncio.to_thread(_sync_stream)
        for chunk in stream:
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            if text := delta.get("content"):
                yield f"data: {text}\n\n".encode()
        yield b"event: done\ndata: [DONE]\n\n"
    except Exception as e:
        logger.exception("Legacy streaming failed: %s", e)
        yield f"event: error\ndata: {e}\n\n".encode()

# ------------------------------ Endpoints ------------------------------ #
@router.post("/respond", response_model=PromptResponse, summary="Respond to prompt")
async def auto_reply_bot(body: PromptRequest, current_user: User = Depends(get_current_user)):
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="AI not configured (missing API key).")

    key = f"{current_user.id}|{current_user.email}"
    if not _rate_ok(key):
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")

    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")

    model, temp, max_tok = _clamp_params(body.model, body.temperature, body.max_tokens)
    msgs = _messages(current_user, body.language, body.system_prompt, body.prompt.strip())

    if body.stream:
        return StreamingResponse(
            _stream_sse(msgs, model, temp, max_tok),
            media_type="text/event-stream"
        )

    text, usage = await _chat_once(msgs, model, temp, max_tok)
    return JSONResponse(PromptResponse(
        user=current_user.email,
        plan=current_user.subscription_status,
        prompt=body.prompt.strip(),
        response=text,
        model=model,
        usage=usage or None
    ).model_dump())

@router.get("/pro-chatbot", summary="Pro/Business access test")
async def use_pro_feature(current_user: User = Depends(get_current_user)):
    return {
        "message": f"✅ Welcome {current_user.full_name}, Pro feature access granted.",
        "plan": current_user.subscription_status,
        "feature": "AI Auto-Responder"
    }
