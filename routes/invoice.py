# backend/routes/invoice_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import List, Optional, Literal, Dict, Any
import base64
import uuid

from fastapi import APIRouter, HTTPException, Header, Query, status
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field, ConfigDict, PositiveInt

from backend.services.pdf_service import send_invoice

router = APIRouter(prefix="/invoices", tags=["Invoices"])

# ---------- Schemas (Pydantic v2) ----------
class InvoiceItem(BaseModel):
    description: str = Field(..., min_length=1, max_length=256)
    quantity: PositiveInt = Field(..., description="Integer quantity >= 1")
    unit_price: Decimal = Field(..., ge=0, description="Unit price in currency minor precision (e.g., 2 dp)")

class Customer(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: Optional[str] = Field(None, max_length=255)

class InvoiceData(BaseModel):
    customer: Customer
    currency: str = Field("TZS", min_length=3, max_length=3, description="ISO 4217 currency code (e.g., TZS, USD)")
    items: List[InvoiceItem] = Field(..., min_items=1, max_items=200)
    notes: Optional[str] = Field(None, max_length=2000)
    description: Optional[str] = Field(None, max_length=256, description="Short summary for templates that need it")
    due_date: Optional[date] = None
    tax_percent: Decimal = Field(0, ge=0, le=100, description="Percent 0–100")
    discount_percent: Decimal = Field(0, ge=0, le=100, description="Percent 0–100")

class InvoiceCreate(BaseModel):
    template_id: str = Field(..., min_length=1)
    data: InvoiceData
    invoice_number: Optional[str] = Field(None, description="Override invoice number; defaults to auto-generated")
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

class InvoiceJSONOut(BaseModel):
    status: str
    invoice_id: str
    invoice_number: str
    currency: str
    subtotal: str
    tax_amount: str
    discount_amount: str
    total: str
    service_result: Dict[str, Any] = Field(default_factory=dict)

# ---------- Utils ----------
def _q2(v: Decimal) -> Decimal:
    """Quantize to 2 decimal places using bankers' rounds (HALF_UP for money)."""
    try:
        return (Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Invalid monetary value")

def _compute_totals(items: List[InvoiceItem], tax_percent: Decimal, discount_percent: Decimal):
    subtotal = sum((Decimal(i.quantity) * _q2(i.unit_price) for i in items), Decimal("0"))
    subtotal = _q2(subtotal)
    tax_amount = _q2(subtotal * (Decimal(tax_percent) / Decimal("100")))
    discount_amount = _q2(subtotal * (Decimal(discount_percent) / Decimal("100")))
    total = _q2(subtotal + tax_amount - discount_amount)
    return subtotal, tax_amount, discount_amount, total

def _auto_invoice_number() -> str:
    # Example: INV-YYYYMM-<short-uuid>
    short = uuid.uuid4().hex[:8].upper()
    from datetime import datetime as _dt
    return f"INV-{_dt.utcnow():%Y%m}-{short}"

def _build_payload(body: InvoiceCreate, totals: tuple[Decimal, Decimal, Decimal, Decimal]) -> Dict[str, Any]:
    subtotal, tax_amount, discount_amount, total = totals
    d = body.data
    # Many PDF services prefer strings for amounts—avoid float serialization issues
    payload = {
        "template": {"id": body.template_id},
        "data": {
            "invoice_number": body.invoice_number or _auto_invoice_number(),
            "currency": d.currency,
            "customer_name": d.customer.name,
            "customer_email": d.customer.email,
            "description": d.description or "Invoice",
            "notes": d.notes,
            "due_date": d.due_date.isoformat() if d.due_date else None,
            "items": [
                {
                    "description": it.description,
                    "quantity": int(it.quantity),
                    "unit_price": str(_q2(it.unit_price)),
                    "line_total": str(_q2(Decimal(it.quantity) * _q2(it.unit_price))),
                }
                for it in d.items
            ],
            "subtotal": str(subtotal),
            "tax_percent": str(_q2(d.tax_percent)),
            "tax_amount": str(tax_amount),
            "discount_percent": str(_q2(d.discount_percent)),
            "discount_amount": str(discount_amount),
            "total": str(total),
            "metadata": body.metadata,
        },
    }
    return payload

# ---------- Endpoint ----------
@router.post(
    "/generate",
    summary="Generate an invoice (JSON or PDF stream)",
    responses={
        200: {"description": "JSON envelope with service result"},
        201: {"description": "PDF file stream", "content": {"application/pdf": {}}},
        400: {"description": "Bad Request"},
        422: {"description": "Validation Error"},
        502: {"description": "Downstream Service Error"},
        500: {"description": "Server Error"},
    },
)
def generate_invoice(
    body: InvoiceCreate,
    response_mode: Literal["json", "file", "base64"] = Query(
        "json", description="Return JSON (default), raw PDF file stream, or base64"
    ),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", description="Prevents accidental duplicates"
    ),
):
    """
    Generates an invoice using the selected template and line items.

    - Computes money with `Decimal` to avoid float errors.
    - Supports `Idempotency-Key` forwarding (if your `send_invoice` honors it).
    - `response_mode=file` streams a PDF if available; otherwise falls back to JSON.
    - `response_mode=base64` returns a base64-encoded PDF if provided by the service.
    """
    # Compute totals
    subtotal, tax_amount, discount_amount, total = _compute_totals(
        body.data.items, body.data.tax_percent, body.data.discount_percent
    )

    # Build payload for the PDF service
    payload = _build_payload(body, (subtotal, tax_amount, discount_amount, total))

    # Call downstream service (be liberal with response interpretation)
    try:
        try:
            result = send_invoice(payload, idempotency_key=idempotency_key)  # type: ignore[call-arg]
        except TypeError:
            # Backward-compat: older service signature
            result = send_invoice(payload)
    except HTTPException:
        raise
    except Exception as e:
        # Map unknown errors to 502 (bad gateway) to indicate downstream failure
        raise HTTPException(status_code=502, detail=f"Invoice service error: {e}")

    invoice_number = payload["data"]["invoice_number"]
    invoice_id = str(uuid.uuid4())

    # Try to handle different service result shapes
    # 1) Raw bytes (PDF):
    if isinstance(result, (bytes, bytearray)):
        if response_mode == "file":
            return StreamingResponse(
                iter([result]),
                media_type="application/pdf",
                status_code=status.HTTP_201_CREATED,
                headers={"Content-Disposition": f'inline; filename="{invoice_number}.pdf"'},
            )
        elif response_mode == "base64":
            b64 = base64.b64encode(result).decode("ascii")
            return JSONResponse(
                content={
                    "status": "success",
                    "invoice_id": invoice_id,
                    "invoice_number": invoice_number,
                    "currency": body.data.currency,
                    "subtotal": str(subtotal),
                    "tax_amount": str(tax_amount),
                    "discount_amount": str(discount_amount),
                    "total": str(total),
                    "pdf_base64": b64,
                    "service_result": {"type": "bytes"},
                }
            )

    # 2) Dict result (commonly: { pdf_url, pdf_base64, html, id, ... })
    if isinstance(result, dict):
        # If user asked for file and we have base64, stream it
        if response_mode in {"file", "base64"} and "pdf_base64" in result:
            try:
                pdf_bytes = base64.b64decode(result["pdf_base64"])
            except Exception:
                pdf_bytes = None
            if pdf_bytes:
                if response_mode == "file":
                    return StreamingResponse(
                        iter([pdf_bytes]),
                        media_type="application/pdf",
                        status_code=status.HTTP_201_CREATED,
                        headers={"Content-Disposition": f'inline; filename="{invoice_number}.pdf"'},
                    )
                else:
                    # base64 passthrough
                    return JSONResponse(
                        content={
                            "status": "success",
                            "invoice_id": invoice_id,
                            "invoice_number": invoice_number,
                            "currency": body.data.currency,
                            "subtotal": str(subtotal),
                            "tax_amount": str(tax_amount),
                            "discount_amount": str(discount_amount),
                            "total": str(total),
                            "pdf_base64": result["pdf_base64"],
                            "service_result": {k: v for k, v in result.items() if k != "pdf_base64"},
                        }
                    )

        # Otherwise return JSON envelope
        return JSONResponse(
            content=InvoiceJSONOut(
                status="success",
                invoice_id=invoice_id,
                invoice_number=invoice_number,
                currency=body.data.currency,
                subtotal=str(subtotal),
                tax_amount=str(tax_amount),
                discount_amount=str(discount_amount),
                total=str(total),
                service_result=result,
            ).model_dump()
        )

    # 3) Unknown type -> return metadata only
    return JSONResponse(
        content=InvoiceJSONOut(
            status="success",
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            currency=body.data.currency,
            subtotal=str(subtotal),
            tax_amount=str(tax_amount),
            discount_amount=str(discount_amount),
            total=str(total),
            service_result={"raw_type": str(type(result))},
        ).model_dump()
    )
