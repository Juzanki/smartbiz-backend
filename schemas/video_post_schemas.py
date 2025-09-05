# -*- coding: utf-8 -*-
# Auto-generated placeholder by restore_missing_schemas.py on 2025-08-23T03:50:38.189305+00:00
from __future__ import annotations

from typing import Optional, List, Dict, Any
from datetime import datetime
from ._compat import BaseModel, Field, ConfigDict, P2


class PlaceholderSchema(BaseModel):
    pass

    if P2:
        model_config = ConfigDict(from_attributes=True, extra="ignore")
    else:
        class Config:  # type: ignore
            orm_mode = True
            extra = "ignore"
