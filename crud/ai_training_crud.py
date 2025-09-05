from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models import KeywordTraining
from typing import List

# --- Create Keyword Training ---


def create_training(
        db: Session,
        user_id: int,
        keyword: str,
        response: str) -> KeywordTraining:
    entry = KeywordTraining(
        user_id=user_id,
        keyword=keyword.lower(),
        response=response
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry

# --- Get All Keyword Trainings ---


def get_all_trainings(db: Session, user_id: int) -> List[KeywordTraining]:
    return db.query(KeywordTraining).filter(
        KeywordTraining.user_id == user_id).all()

# --- Get Response by Keyword ---


def get_response_by_keyword(
        db: Session,
        user_id: int,
        keyword: str) -> KeywordTraining:
    return db.query(KeywordTraining).filter(
        KeywordTraining.user_id == user_id,
        KeywordTraining.keyword == keyword.lower()
    ).first()


