# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Generic, List, Optional, TypeVar
from datetime import datetime

from ._compat import BaseModel, Field, ConfigDict, P2

T = TypeVar("T")

class MessageOut(BaseModel):
    message: str = Field(..., description="Human-readable message")
    if P2:
        model_config = ConfigDict(from_attributes=True, extra="ignore")
    else:
        class Config:  # type: ignore
            orm_mode = True
            extra = "ignore"

class PageParams(BaseModel):
    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=200)
    if P2:
        model_config = ConfigDict(extra="ignore")
    else:
        class Config:  # type: ignore
            extra = "ignore"

class PageResult(Generic[T], BaseModel):
    page: int
    size: int
    total: int
    items: List[T]
    if P2:
        model_config = ConfigDict(from_attributes=True, extra="ignore")
    else:
        class Config:  # type: ignore
            orm_mode = True
            extra = "ignore"
