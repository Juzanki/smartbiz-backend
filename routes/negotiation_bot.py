# backend/routes/negotiation_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import json
from math import floor
from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Header, status
from pydantic import BaseModel, Field, ConfigDict, PositiveFloat, constr
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.config import settings
from backend.utils.ai import get_openai_client, chat_complete, MissingAPIKey

router = APIRouter(prefix="/ai", tags=["AI Negotiation"])

# ---------- Tunable policy (env-driven, with safe defaults) ----------
DEFAULT_MODEL = (
    getattr(settings, "OPENAI_MODEL", None)
    or os.getenv("OPENAI_MODEL")
    or "gpt-4o-mini"
)
MAX_DISCOUNT_PCT   = float(getattr(settings, "NEGOTIATION_MAX_DISCOUNT_PCT", 15))  # e.g., 15%
MIN_PRICE_ABSOLUTE = float(getattr(settings, "NEGOTIATION_MIN_TZS", 0))            # absolute floor
PRICE_STEP         = int(getattr(settings, "NEGOTIATION_ROUND_STEP", 100))         # round to nearest TZS 100
TZS = "TZS"

# ---------- Schemas ----------
class NegotiationRequest(BaseModel):
    product_name: constr(strip_whitespace=True, min_length=1, max_length=120)
    initial_price: PositiveFloat = Field(..., description="Sticker price in TZS")
    message: constr(strip_whitespace=True, min_length=1, max_length=1000)

class NegotiationOut(BaseModel):
    product: str
    currency: str = TZS
    initial_price: float
    min_acceptable_price: float
    offer_price: float
    bot_response: str
    usage: Dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(from_attributes=True)

# ---------- Helpers ----------
def _floor_price(initial_price: float) -> float:
    """
    Floor based on max discount + optional absolute min.
    Rounded down to configured step.
    """
    pct_floor = initial_price * (1 - MAX_DISCOUNT_PCT / 100.0)
    floor_price = max(pct_floor, MIN_PRICE_ABSOLUTE)
    step = max(1, PRICE_STEP)
    rounded = floor(floor_price / step) * step
    return float(max(rounded, 0.0))

def _system_prompt(min_price: float) -> str:
    return (
        "You are a concise, polite store negotiation bot. "
        "Negotiate fairly while protecting margin. "
        f"Prices are in {TZS}. Never offer below the provided min_acceptable_price. "
        "Be friendly, brief (<= 80 words), and if you concede, give a short reason. "
        "If the customer is rude, stay calm and professional."
    )

def _json_schema() -> Dict[str, Any]:
    """Structured output schema for Responses API."""
    return {
        "name": "NegotiationReply",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "reply": {"type": "string", "minLength": 1, "maxLength": 500},
                "offer_price": {"type": "number", "minimum": 0},
                "concessions": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                "reason": {"type": "string", "minLength": 1, "maxLength": 400},
            },
            "required": ["reply", "offer_price"],
        },
        "strict": True,
    }

# ---------- Endpoint ----------
@router.post(
    "/negotiate",
    response_model=NegotiationOut,
    summary="Negotiate with AI bot (structured, policy-aware)",
)
def negotiate_price(
    request: NegotiationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key",
        description="Optional idempotency key to dedupe client retries"
    ),
):
    """
    Generates a short negotiation reply and a concrete offer_price
    that respects your discount policy. Uses Responses API if available,
    otherwise falls back to Chat Completions.
    """
    min_price = _floor_price(request.initial_price)
    if min_price > request.initial_price:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Configuration error: min acceptable price exceeds initial price.",
        )

    system = _system_prompt(min_price)
    user_payload = {
        "product_name": request.product_name,
        "currency": TZS,
        "initial_price": request.initial_price,
        "min_acceptable_price": min_price,
        "customer_message": request.message,
    }

    # Try modern Responses API first
    try:
        client = get_openai_client()  # raises MissingAPIKey if key missing
        if hasattr(client, "responses"):  # OpenAI SDK >= 1.0 (Responses API available)
            resp = client.responses.create(
                model=DEFAULT_MODEL,
                input=(
                    f"{system}\n\n"
                    "Return JSON that fits the schema. Context:\n"
                    f"{json.dumps(user_payload, ensure_ascii=False)}"
                ),
                response_format={"type": "json_schema", "json_schema": _json_schema()},
                max_output_tokens=int(getattr(settings, "OPENAI_MAX_TOKENS", 256)),
                extra_headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
            )
            raw = getattr(resp, "output_text", None)
            data = json.loads(raw) if raw else {}
            offer_price = float(data.get("offer_price", min_price))
            reply = (
                str(data.get("reply", "")).strip()
                or "Thanks for your interest. Could you share your target budget?"
            )
            usage = dict(getattr(resp, "usage", {}) or {})
        else:
            # Fallback: Chat Completions wrapper (mobile-first timeouts/retries)
            schema_hint = (
                "Return JSON with keys: reply (string), offer_price (number). "
                f"Never go below min_acceptable_price={min_price}."
            )
            text = chat_complete(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": schema_hint},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                model=DEFAULT_MODEL,
                max_tokens=int(getattr(settings, "OPENAI_MAX_TOKENS", 256)),
                temperature=float(getattr(settings, "OPENAI_TEMPERATURE", 0.4)),
                timeout=int(getattr(settings, "OPENAI_REQUEST_TIMEOUT", 25)),
                retries=int(getattr(settings, "OPENAI_RETRIES", 2)),
            )
            # Best-effort JSON parse
            try:
                data = json.loads(text)
                reply = str(data.get("reply", "")).strip()
                offer_price = float(data.get("offer_price", min_price))
            except Exception:
                # If model returned prose, craft minimal reply & safe offer
                reply = text.strip()[:500]
                offer_price = min_price
            usage = {}  # not available in wrapper path

        # Server-side enforcement: floor + rounding
        offer_price = max(min_price, offer_price)
        step = max(1, PRICE_STEP)
        offer_price = float(round(offer_price / step) * step)

        return NegotiationOut(
            product=request.product_name,
            currency=TZS,
            initial_price=float(request.initial_price),
            min_acceptable_price=min_price,
            offer_price=offer_price,
            bot_response=reply,
            usage=usage,
        )

    except MissingAPIKey as e:
        # Service not configured; do NOT crash the app
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Negotiation bot failed: {e}")

