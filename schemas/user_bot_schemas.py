# -*- coding: utf-8 -*-
# Auto-generated placeholder by restore_missing_schemas.py on 2025-08-23T03:25:43.722362+00:00
from __future__ import annotations

from typing import Optional, List, Dict, Any
from datetime import datetime
from ._compat import BaseModel, Field, ConfigDict, P2


class UserBotCreate(BaseModel):
    stream_id: int = Field(..., description='Target stream id')
    gift_name: str = Field(..., description='Gift display name')

    if P2:
        model_config = ConfigDict(from_attributes=True, extra="ignore")
    else:
        class Config:  # type: ignore
            orm_mode = True
            extra = "ignore"

class UserBotOut(BaseModel):
    id: int = Field(..., description='Primary id')
    stream_id: Optional[int] = None
    user_id: Optional[int] = None
    gift_name: Optional[str] = None
    sent_at: Optional[datetime] = None

    if P2:
        model_config = ConfigDict(from_attributes=True, extra="ignore")
    else:
        class Config:  # type: ignore
            orm_mode = True
            extra = "ignore"
