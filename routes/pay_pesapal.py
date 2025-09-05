# backend/routes/pay_pesapal.py
"""
Pesapal payment start (redirect) + callback handling (GET/POST) with sane defaults.
- Works locally and on Railway
- Validates inputs
- Supports idempotency key header (optional)
- 303 redirect for POST->GET after creating payment session
- Optional HMAC signature check for callbacks (adjust to Pesapal's spec)
"""
from __future__ import annotations
import os
import hmac
import hashlib
from typing import Optional

from fastapi import APIRouter, Request, Header, HTTPException, status, Query
from fastapi.responses import RedirectResponse, PlainTextResponse
from pydantic import BaseModel, Field, EmailStr, condecimal

router = APIRouter(tags=["Pesapal"])

# ---------- ENV & Helpers ----------

# Base URL to construct callback if PESAPAL_CALLBACK_URL missing
_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("RAILWAY_PUBLIC_URL")
    or "http://127.0.0.1:8000"
).rstrip("/")

PESAPAL_CALLBACK_URL = (os.getenv("PESAPAL_CALLBACK_URL") or f"{_BASE_URL}/wallet/pesapal/callback").strip()
PESAPAL_CONSUMER_KEY = (os.getenv("PESAPAL_CONSUMER_KEY") or "").strip()
PESAPAL_CONSUMER_SECRET = (os.getenv("PESAPAL_CONSUMER_SECRET") or "").strip()

# If your webhook is signed, keep the secret (adjust naming to your setup)
PESAPAL_WEBHOOK_SECRET = (os.getenv("PESAPAL_WEBHOOK_SECRET") or PESAPAL_CONSUMER_SECRET).encode("utf-8")


def _require_env(var: str, value: str):
    if not value:
        raise RuntimeError(f"Missing env variable: {var}")
    return value


# ---------- Schemas ----------

class PesapalInit(BaseModel):
    amount: condecimal(gt=0, max_digits=12, decimal_places=2) = Field(..., example="3500.00")
    currency: str = Field("TZS", min_length=2, max_length=5)
    account_reference: str = Field(..., min_length=1, max_length=64, description="Order/invoice reference")
    description: Optional[str] = Field(None, max_length=140)
    customer_name: Optional[str] = Field(None, max_length=100)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, min_length=6, max_length=20)

    # In real world you may include return_url/callback override per request
    # return_url: Optional[AnyHttpUrl] = None


# ---------- Routes ----------

@router.post("/pay/pesapal", summary="Start Pesapal Payment (redirect)")
async def start_pesapal_payment(
    payload: PesapalInit,
    request: Request,
    x_idempotency_key: Optional[str] = Header(default=None, convert_underscores=False),
):
    """
    1) Normally: create an order with Pesapal API using CONSUMER_KEY/SECRET
    2) Receive a redirect URL from Pesapal
    3) Redirect client with 303 (POST -> GET)
    """
    # Ensure required envs for real integration
    _require_env("PESAPAL_CONSUMER_KEY", PESAPAL_CONSUMER_KEY)
    _require_env("PESAPAL_CONSUMER_SECRET", PESAPAL_CONSUMER_SECRET)

    # TODO: implement real API call (get token, create order, receive redirect_url)
    # For now, we simulate a redirect URL carrying your account reference
    mock_reference = payload.account_reference
    redirect_url = (
        "https://pay.pesapal.com/mock/redirect"
        f"?reference={mock_reference}"
        f"&amount={payload.amount}"
        f"&currency={payload.currency}"
        f"&callback={PESAPAL_CALLBACK_URL}"
    )

    # 303 ensures browser switches to GET request on target URL
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


# Pesapal/hook senders sometimes call back with GET query params; support both.

@router.get("/wallet/pesapal/callback", status_code=200, summary="Pesapal callback (GET)")
async def pesapal_callback_get(
    merchant_reference: Optional[str] = Query(default=None),
    order_tracking_id: Optional[str] = Query(default=None),
    status_: Optional[str] = Query(default=None, alias="status"),
):
    """
    Handle GET callback: read URL params and update your order status.
    """
    # TODO: verify with Pesapal's order status API if needed
    return {
        "ok": True,
        "via": "GET",
        "merchant_reference": merchant_reference,
        "order_tracking_id": order_tracking_id,
        "status": status_,
    }


@router.post("/wallet/pesapal/callback", status_code=200, summary="Pesapal callback (POST)")
async def pesapal_callback_post(
    request: Request,
    x_pesapal_signature: Optional[str] = Header(default=None, convert_underscores=False),
):
    """
    Handle POST callback/webhook. If your webhook is signed, verify HMAC.
    Adjust header name & algorithm per Pesapal's official docs.
    """
    raw = await request.body()
    body_text = raw.decode("utf-8", errors="replace")

    # OPTIONAL: verify signature (example HMAC-SHA256)
    if x_pesapal_signature:
        digest = hmac.new(PESAPAL_WEBHOOK_SECRET, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, x_pesapal_signature):
            # If signature invalid, reject with 401
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # TODO: parse JSON/XML per Pesapal spec and update order in DB idempotently
    # Respond fast to avoid retries; use 200/204
    return {"ok": True, "via": "POST", "received": body_text[:2000]}


# Optional: a tiny ping route, useful for debugging
@router.get("/wallet/pesapal/callback/ping", response_class=PlainTextResponse, include_in_schema=False)
async def pesapal_callback_ping():
    return "OK"
