from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models import ThemeSetting
from typing import Optional

# --- Save or Update Theme Settings ---


def save_theme(
        db: Session,
        user_id: int,
        primary_color: str,
        secondary_color: str,
        logo_url: Optional[str] = None,
        mode: str = 'light') -> ThemeSetting:
    setting = db.query(ThemeSetting).filter(
        ThemeSetting.user_id == user_id).first()
    if setting:
        setting.primary_color = primary_color
        setting.secondary_color = secondary_color
        setting.logo_url = logo_url
        setting.mode = mode
    else:
        setting = ThemeSetting(
            user_id=user_id,
            primary_color=primary_color,
            secondary_color=secondary_color,
            logo_url=logo_url,
            mode=mode
        )
        db.add(setting)
    db.commit()
    db.refresh(setting)
    return setting

# --- Get Theme Settings ---


def get_theme(db: Session, user_id: int) -> ThemeSetting:
    return db.query(ThemeSetting).filter(
        ThemeSetting.user_id == user_id).first()


