# -*- coding: utf-8 -*-
# Auto-generated placeholder by restore_missing_schemas.py on 2025-08-23T03:50:37.883714+00:00
from __future__ import annotations

from typing import Optional, List, Dict, Any
from datetime import datetime
from ._compat import BaseModel, Field, ConfigDict, P2


class GiftMovementCreate(BaseModel):
    stream_id: int = Field(..., description='Target stream id')
    gift_name: str = Field(..., description='Gift display name')

    if P2:
        model_config = ConfigDict(from_attributes=True, extra="ignore")
    else:
        class Config:  # type: ignore
            orm_mode = True
            extra = "ignore"
