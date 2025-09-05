from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models import TranslationRecord
from datetime import datetime
from typing import List

# --- Log Translation ---


def log_translation(
        db: Session,
        user_id: int,
        original_text: str,
        translated_text: str,
        source_lang: str,
        target_lang: str) -> TranslationRecord:
    entry = TranslationRecord(
        user_id=user_id,
        original_text=original_text,
        translated_text=translated_text,
        source_lang=source_lang,
        target_lang=target_lang,
        translated_at=datetime.utcnow()
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry

# --- Get User Translation History ---


def get_user_translations(
        db: Session,
        user_id: int) -> List[TranslationRecord]:
    return db.query(TranslationRecord).filter(
        TranslationRecord.user_id == user_id).order_by(
        TranslationRecord.translated_at.desc()).all()


