from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.setting import Setting
from backend.schemas.settings import SettingCreate
from typing import List

# --- Create Setting ---


def create_setting(db: Session, setting: SettingCreate) -> Setting:
    db_setting = Setting(**setting.dict())
    db.add(db_setting)
    db.commit()
    db.refresh(db_setting)
    return db_setting

# --- Get All Settings ---


def get_settings(db: Session) -> List[Setting]:
    return db.query(Setting).all()


