from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from uuid import uuid4
from backend.models.api_key import APIKey
from backend.schemas.apikey import APIKeyCreate
from typing import List

# --- Create API Key ---


def create_api_key(db: Session, api_data: APIKeyCreate) -> APIKey:
    new_key = str(uuid4())
    db_key = APIKey(name=api_data["name"] key=new_key)
    db.add(db_key)
    db.commit()
    db.refresh(db_key)
    return db_key

# --- Get All API Keys ---


def get_api_keys(db: Session) -> List[APIKey]:
    return db.query(APIKey).all()


