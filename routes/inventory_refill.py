# backend/routes/inventory_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Optional, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.models.product import Product
from backend.utils.inventory_ai import check_refill_needed, check_stock_levels

router = APIRouter(prefix="/inventory", tags=["Inventory"])

# ---------- Response Schemas (Pydantic v2) ----------
class RecommendationsEnvelope(BaseModel):
    recommendations: List[dict[str, Any]] = Field(default_factory=list)
    count: int = 0
    model_config = ConfigDict(from_attributes=True)

class SuggestionsEnvelope(BaseModel):
    suggestions: List[dict[str, Any]] | List[Any] = Field(default_factory=list)
    model_config = ConfigDict(from_attributes=True)

# ---------- Routes ----------
@router.get(
    "/stock-alerts",
    response_model=RecommendationsEnvelope,
    summary="Smart inventory refill suggestions",
)
def smart_inventory_check(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(0, ge=0, le=10000, description="Optional cap on number of products scanned (0 = no cap)"),
):
    """
    Scan products and return refill suggestions.
    Admin/Owner: scans all products. Others: scans all (adjust here if you scope by owner/business).
    """
    query = db.query(Product)
    if limit:
        query = query.limit(limit)

    suggestions: List[dict[str, Any]] = []
    for product in query.all():
        try:
            suggestion = check_refill_needed(product)
        except Exception:
            # Defensive: skip problematic products but keep the endpoint resilient
            suggestion = None
        if suggestion:
            # Expecting dict-like output from your AI helper; keep it passthrough
            suggestions.append(suggestion)

    return RecommendationsEnvelope(recommendations=suggestions, count=len(suggestions))


@router.get(
    "/suggestions",
    response_model=SuggestionsEnvelope,
    summary="Suggest restocking for fast-selling products",
)
def suggest_inventory_refill(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    top_n: int = Query(20, ge=1, le=200, description="How many top suggestions to return"),
):
    """
    Business-scoped restock suggestions for the current user.
    Requires the user to have a linked business profile.
    """
    if not getattr(current_user, "business_name", None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Business not linked to user profile",
        )

    try:
        # Your helper should already consider sales velocity and stock thresholds.
        suggestions = check_stock_levels(db, current_user.id, top_n=top_n)  # type: ignore[arg-type]
    except TypeError:
        # Backward compatibility if helper doesn't accept top_n
        suggestions = check_stock_levels(db, current_user.id)  # type: ignore[arg-type]
        # If it's a long list, trim client-side for mobile
        if isinstance(suggestions, list):
            suggestions = suggestions[:top_n]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch suggestions: {exc}") from exc

    return SuggestionsEnvelope(suggestions=suggestions)

