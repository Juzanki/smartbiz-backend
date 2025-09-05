# backend/routes/owner_ai_console.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Dict, Any, Tuple

from fastapi import APIRouter, Depends, HTTPException, Header, status, Request
from pydantic import BaseModel, Field, ConfigDict, constr
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.db import get_db
from backend.dependencies import get_current_user
from backend.models.user import User

# Optional models (router still works if some don't exist)
try:
    from backend.models.order import Order
    from backend.models.product import Product
except Exception:  # pragma: no cover
    Order = None  # type: ignore
    Product = None  # type: ignore

try:
    from backend.models.feature_flag import FeatureFlag  # name:str, enabled:bool, updated_at
except Exception:  # pragma: no cover
    FeatureFlag = None  # type: ignore

# ---- OpenAI: use the modern Responses API ----
try:
    from openai import OpenAI
except Exception as e:  # pragma: no cover
    raise RuntimeError("The `openai` Python SDK is required. `pip install openai`") from e

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

client = OpenAI(api_key=OPENAI_API_KEY)
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

router = APIRouter(prefix="/owner", tags=["Owner AI Console"])

# ---------- Schemas ----------
class AICommandRequest(BaseModel):
    prompt: constr(strip_whitespace=True, min_length=1, max_length=4000)

class ToolResult(BaseModel):
    name: str
    ok: bool = True
    data: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class AICommandResponse(BaseModel):
    message: str
    intent: str
    args: Dict[str, Any] = Field(default_factory=dict)
    tool_result: Optional[ToolResult] = None
    usage: Dict[str, Any] = Field(default_factory=dict)

# ---------- Helpers ----------
UTC_NOW = lambda: datetime.now(timezone.utc)

def _assert_owner(user: User) -> None:
    if getattr(user, "role", None) != "owner":
        raise HTTPException(status_code=403, detail="Access restricted to owner only")

def _money(v: Any) -> str:
    try:
        return str(Decimal(v).quantize(Decimal("0.01")))
    except Exception:
        return "0.00"

def _structured_schema() -> Dict[str, Any]:
    """
    Force the model to return a strict JSON object describing the planned action.
    """
    return {
        "name": "OwnerConsolePlan",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [
                        "answer",
                        "get_kpis",
                        "find_user",
                        "list_recent_orders",
                        "set_feature_flag"   # requires confirmation header server-side
                    ]
                },
                "args": {"type": "object"},
                "final_message": {"type": "string", "minLength": 1}
            },
            "required": ["intent", "final_message"]
        }
    }

def _system_prompt() -> str:
    return (
        "You are an executive backend assistant for the store OWNER. "
        "Keep answers concise and actionable. You may propose calling one of these intents: "
        "get_kpis(), find_user(identifier), list_recent_orders(limit<=20), set_feature_flag(name, enabled). "
        "For destructive changes (like set_feature_flag), do NOT describe raw SQL or secretsâ€”just set intent and args. "
        "Always return a very short explanation in 'final_message'."
    )

# ---------- Tool implementations (safe by default) ----------
def tool_get_kpis(db: Session) -> ToolResult:
    users_total = db.query(func.count(User.id)).scalar() or 0
    users_active = users_total
    if hasattr(User, "is_active"):
        users_active = db.query(func.count(User.id)).filter(User.is_active.is_(True)).scalar() or 0

    orders_total = 0
    orders_pending = 0
    gm_subtotal = "0.00"

    if Order:
        q = db.query(Order)
        orders_total = q.count()
        if hasattr(Order, "status"):
            orders_pending = db.query(func.count(Order.id)).filter(Order.status == "pending").scalar() or 0
        # gross merchandise (sum of totals or prices)
        if hasattr(Order, "total"):
            s = db.query(func.coalesce(func.sum(Order.total), 0)).scalar()
        else:
            s = db.query(func.coalesce(func.sum(Order.price), 0)).scalar()
        gm_subtotal = _money(s)

    products_total = 0
    if Product:
        products_total = db.query(func.count(Product.id)).scalar() or 0

    return ToolResult(
        name="get_kpis",
        data=dict(
            users_total=int(users_total),
            users_active=int(users_active),
            orders_total=int(orders_total),
            orders_pending=int(orders_pending),
            gm_subtotal=gm_subtotal,
            products_total=int(products_total),
            ts=UTC_NOW().isoformat()
        )
    )

def tool_find_user(db: Session, identifier: str) -> ToolResult:
    u = (
        db.query(User)
        .filter(
            (User.username == identifier) |
            (getattr(User, "email", None) == identifier)  # safe if column exists; None short-circuits
        )
        .first()
    )
    if not u:
        return ToolResult(name="find_user", ok=False, error="User not found")
    return ToolResult(
        name="find_user",
        data=dict(
            id=getattr(u, "id"),
            username=getattr(u, "username", None),
            email=getattr(u, "email", None),
            role=getattr(u, "role", None),
            is_active=getattr(u, "is_active", True),
            created_at=getattr(u, "created_at", None)
        )
    )

def tool_list_recent_orders(db: Session, limit: int = 10) -> ToolResult:
    if not Order:
        return ToolResult(name="list_recent_orders", ok=False, error="Order model not available")
    limit = max(1, min(limit, 20))
    col = getattr(Order, "created_at", getattr(Order, "id"))
    rows = (
        db.query(Order)
        .order_by(col.desc())
        .limit(limit)
        .all()
    )
    out = []
    for o in rows:
        out.append(dict(
            id=getattr(o, "id"),
            user_id=getattr(o, "user_id", None),
            status=str(getattr(o, "status", "pending")),
            total=_money(getattr(o, "total", getattr(o, "price", 0))),
            created_at=getattr(o, "created_at", None)
        ))
    return ToolResult(name="list_recent_orders", data={"orders": out})

def tool_set_feature_flag(db: Session, name: str, enabled: bool) -> ToolResult:
    if not FeatureFlag:
        return ToolResult(name="set_feature_flag", ok=False, error="FeatureFlag model not available")
    ff = db.query(FeatureFlag).filter(FeatureFlag.name == name).first()
    if not ff:
        ff = FeatureFlag(name=name, enabled=bool(enabled), updated_at=UTC_NOW())
        db.add(ff)
    else:
        ff.enabled = bool(enabled)
        if hasattr(ff, "updated_at"):
            ff.updated_at = UTC_NOW()
    db.commit()
    db.refresh(ff)
    return ToolResult(name="set_feature_flag", data=dict(name=ff.name, enabled=bool(ff.enabled)))

# ---------- Dispatcher ----------
def dispatch_tool(db: Session, intent: str, args: Dict[str, Any], owner_confirmed: bool) -> ToolResult:
    if intent == "get_kpis":
        return tool_get_kpis(db)
    if intent == "find_user":
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return ToolResult(name="find_user", ok=False, error="identifier is required")
        return tool_find_user(db, identifier)
    if intent == "list_recent_orders":
        limit = int(args.get("limit", 10))
        return tool_list_recent_orders(db, limit=limit)
    if intent == "set_feature_flag":
        # Never perform state-changing actions unless explicitly confirmed by the owner
        if not owner_confirmed:
            return ToolResult(name="set_feature_flag", ok=False, error="Confirmation required. Resend with X-Owner-Confirm: true")
        name = str(args.get("name", "")).strip()
        enabled = bool(args.get("enabled", False))
        if not name:
            return ToolResult(name="set_feature_flag", ok=False, error="name is required")
        return tool_set_feature_flag(db, name, enabled)
    # No tool selected; return empty result
    return ToolResult(name=intent, ok=False, error="Unknown intent")

# ---------- Route ----------
@router.post("/command", response_model=AICommandResponse, summary="Execute an owner console command via AI")
def execute_owner_command(
    body: AICommandRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    owner_confirm: Optional[str] = Header(default=None, alias="X-Owner-Confirm"),
):
    """
    Owner-only AI console with strict guardrails:
      - Uses OpenAI **Responses API** with **Structured Outputs** to plan an action.
      - Executes only a small, audited set of server-side tools.
      - Any state-changing action (e.g., set_feature_flag) requires the header `X-Owner-Confirm: true`.
    """
    _assert_owner(current_user)

    # Compose the model input
    system = _system_prompt()
    user_msg = body.prompt.strip()

    try:
        resp = client.responses.create(
            model=DEFAULT_MODEL,
            input=f"{system}\n\nUser request:\n{json.dumps({'prompt': user_msg}, ensure_ascii=False)}",
            response_format={"type": "json_schema", "json_schema": _structured_schema()},
            max_output_tokens=400,
            extra_headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
        )
        raw = getattr(resp, "output_text", None)  # concatenated string for structured outputs
        plan = json.loads(raw) if raw else {}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI error: {e}")

    intent = str(plan.get("intent", "answer"))
    args = plan.get("args", {}) or {}
    final_message = str(plan.get("final_message", "")).strip() or "OK."

    # Dispatch (safe by default)
    confirmed = (str(owner_confirm).lower() == "true")
    tool_result: Optional[ToolResult] = None
    if intent != "answer":
        tool_result = dispatch_tool(db, intent, args, owner_confirmed=confirmed)

    # Usage (if available)
    usage = {}
    if getattr(resp, "usage", None):
        usage = dict(resp.usage)  # type: ignore

    return AICommandResponse(
        message=final_message,
        intent=intent,
        args=args,
        tool_result=tool_result,
        usage=usage,
    )


