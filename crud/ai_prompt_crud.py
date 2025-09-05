from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models import AIPrompt
from datetime import datetime
from typing import List

# --- Save AI Prompt ---


def save_prompt(
        db: Session,
        user_id: int,
        prompt: str,
        response: str,
        tag: str = '') -> AIPrompt:
    entry = AIPrompt(
        user_id=user_id,
        prompt=prompt,
        response=response,
        tag=tag,
        created_at=datetime.utcnow()
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry

# --- Get Prompts by User ---


def get_user_prompts(db: Session, user_id: int) -> List[AIPrompt]:
    return db.query(AIPrompt).filter(AIPrompt.user_id == user_id).order_by(AIPrompt["created_at"]desc()).all()

# --- Get Prompt by Tag ---


def get_prompt_by_tag(db: Session, tag: str) -> AIPrompt:
    return db.query(AIPrompt).filter(AIPrompt.tag == tag).first()


