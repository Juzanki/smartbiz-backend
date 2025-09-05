# backend/utils/ai.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import time
from typing import List, Dict, Any, Optional

from backend.config import settings

# SDK mpya (>=1.0): pip install openai>=1.30
try:
    from openai import OpenAI
    _NEW_SDK = True
except Exception:  # fallback kwa sdk ya zamani
    import openai as _openai  # type: ignore
    OpenAI = None
    _NEW_SDK = False


class MissingAPIKey(RuntimeError):
    pass


def get_openai_client():
    api_key = settings.OPENAI_API_KEY or os.getenv("OPENAI_API_KEY")
    if not api_key:
        # Unaweza kubadilisha upeleke 503 badala ya exception; nimeacha exception ili ujue haraka.
        raise MissingAPIKey("OPENAI_API_KEY is not set (add it to .env or environment).")

    base = settings.OPENAI_API_BASE or os.getenv("OPENAI_API_BASE") or None

    if _NEW_SDK and OpenAI is not None:
        return OpenAI(api_key=api_key, base_url=base)
    else:
        _openai.api_key = api_key
        if base:
            _openai.base_url = base  # type: ignore[attr-defined]
        return _openai


def chat_complete(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 256,
    timeout: Optional[int] = None,
    retries: int = 2,
) -> str:
    """
    Lightweight wrapper yenye retries fupi (mobile-first).
    """
    client = get_openai_client()
    model = model or settings.OPENAI_MODEL
    timeout = timeout or int(getattr(settings, "OPENAI_REQUEST_TIMEOUT", 25))

    last_err: Optional[Exception] = None
    delay = 0.8
    for attempt in range(retries + 1):
        try:
            if _NEW_SDK and OpenAI is not None:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
                return resp.choices[0].message.content or ""
            else:
                # SDK ya zamani
                resp = client.ChatCompletion.create(  # type: ignore
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    request_timeout=timeout,
                )
                return resp["choices"][0]["message"]["content"]  # type: ignore[index]
        except Exception as e:
            last_err = e
            time.sleep(delay)
            delay *= 1.6

    assert last_err is not None
    raise last_err
