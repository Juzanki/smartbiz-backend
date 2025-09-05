from __future__ import annotations
from typing import Optional, Literal, Dict, Any
from datetime import datetime
from ._compat import BaseModel, Field, ConfigDict, P2

Status = Literal["open", "in_progress", "resolved", "closed"]

class SupportTicketOut(BaseModel):
    id: int
    user_id: int
    title: str
    body: str
    status: Status = "open"
    meta: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    if P2:
        model_config = ConfigDict(from_attributes=True, extra="ignore")
    else:
        class Config:  # type: ignore
            orm_mode = True
            extra = "ignore"
