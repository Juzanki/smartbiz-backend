from __future__ import annotations
from datetime import datetime
from typing import Optional, Literal
from ._compat import BaseModel, Field, ConfigDict, P2

class BalanceCreate(BaseModel):
    user_id: int
    amount: float = Field(..., ge=-1e12, le=1e12)
    currency: str = Field("USD", max_length=8)
    reason: Optional[str] = None

class BalanceOut(BaseModel):
    id: int
    user_id: int
    amount: float
    currency: str
    type: Optional[Literal["credit", "debit"]] = None
    balance_after: Optional[float] = None
    created_at: Optional[datetime] = None

    if P2:
        model_config = ConfigDict(from_attributes=True, extra="ignore")
    else:
        class Config:  # type: ignore
            orm_mode = True
            extra = "ignore"
