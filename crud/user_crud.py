# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Optional, Any, Dict

try:
    from sqlalchemy.orm import Session
except Exception:
    class Session: ...  # type: ignore

try:
    from backend.models.user import User as UserModel  # type: ignore
    from backend.schemas.user import UserCreate  # type: ignore, UserOut
    try:
        from backend.schemas.user import UserCreate  # type: ignore, UserOut
    except Exception:
        class UserUpdate: ...  # type: ignore
except Exception:
    class User:  # type: ignore
        pass
    class UserCreate:  # type: ignore
        def __init__(self, email: str = "", password: str = "", username: str = "", phone: str = ""):
            self.email = email
            self.password = password
            self.username = username
            self.phone = phone
    class UserUpdate:  # type: ignore
        pass

import hashlib

def _hash_password(p: str) -> str:
    return hashlib.sha256((p or "changeme").encode("utf-8")).hexdigest()

def _first_existing_field(model, names):
    for n in names:
        if hasattr(model, n):
            return n
    return None

def get_user(db: "Session", user_id: int) -> Optional["User"]:
    try:
        return db.query(User).filter(User.id == user_id).first()  # type: ignore[attr-defined]
    except Exception:
        return None

def get_users(db: "Session", skip: int = 0, limit: int = 100) -> List["User"]:
    try:
        return db.query(User).offset(skip).limit(limit).all()  # type: ignore[attr-defined]
    except Exception:
        return []

def create_user(db: "Session", user: "UserCreate") -> "User":
    db_user = User()  # type: ignore[call-arg]
    if hasattr(db_user, "email"):
        setattr(db_user, "email", getattr(user, "email", None))
    if hasattr(db_user, "username"):
        setattr(db_user, "username", getattr(user, "username", None))
    if hasattr(db_user, "phone"):
        setattr(db_user, "phone", getattr(user, "phone", None))
    if hasattr(db_user, "hashed_password"):
        setattr(db_user, "hashed_password", _hash_password(getattr(user, "password", "")))
    if hasattr(db_user, "is_active") and getattr(db_user, "is_active", None) is None:
        setattr(db_user, "is_active", True)
    db.add(db_user)
    db.commit()
    try:
        db.refresh(db_user)
    except Exception:
        pass
    return db_user

def get_user_by_email(db: "Session", email: str) -> Optional["User"]:
    try:
        field = _first_existing_field(User, ["email", "email_address", "emailAddress"])
        if not field:
            return None
        return db.query(User).filter(getattr(User, field) == email).first()  # type: ignore[attr-defined]
    except Exception:
        return None

def get_user_by_username(db: "Session", username: str) -> Optional["User"]:
    try:
        field = _first_existing_field(User, ["username", "user_name", "handle", "name"])
        if not field:
            return None
        return db.query(User).filter(getattr(User, field) == username).first()  # type: ignore[attr-defined]
    except Exception:
        return None

def get_user_by_phone(db: "Session", phone: str) -> Optional["User"]:
    try:
        field = _first_existing_field(User, ["phone", "phone_number", "phoneNumber", "mobile", "msisdn", "tel", "telephone"])
        if not field:
            return None
        return db.query(User).filter(getattr(User, field) == phone).first()  # type: ignore[attr-defined]
    except Exception:
        return None

def _to_payload(update: Any) -> Dict[str, Any]:
    """Saga data kutoka dict au pydantic model (model_dump / dict / attributes)."""
    if update is None:
        return {}
    if isinstance(update, dict):
        return {k: v for k, v in update.items() if v is not None}
    # pydantic v2
    if hasattr(update, "model_dump"):
        return {k: v for k, v in update.model_dump(exclude_unset=True).items() if v is not None}
    # pydantic v1
    if hasattr(update, "dict"):
        return {k: v for k, v in update.dict(exclude_unset=True).items() if v is not None}
    # generic object
    out = {}
    for k in dir(update):
        if not k.startswith("_"):
            try:
                v = getattr(update, k)
                if not callable(v) and v is not None:
                    out[k] = v
            except Exception:
                pass
    return out

def update_user_profile(db: "Session", user_id: int, updates: Any) -> Optional["User"]:
    """
    updates: dict | UserUpdate | object with fields.
    Inasasisha tu fields zilizopo kwenye model yako; hairushi error kama field haipo.
    Pia hushughulikia hashing ya password -> hashed_password ikiwa ipo.
    """
    db_user = db.query(User).filter(User.id == user_id).first()  # type: ignore[attr-defined]
    if not db_user:
        return None

    payload = _to_payload(updates)

    # password -> hashed_password
    password_value = None
    for k in ("password", "new_password", "plain_password"):
        if k in payload and payload[k]:
            password_value = payload[k]
            break
    if password_value and hasattr(db_user, "hashed_password"):
        setattr(db_user, "hashed_password", _hash_password(password_value))

    # logical -> possible column names
    field_map = {
        "email": ["email", "email_address", "emailAddress"],
        "username": ["username", "user_name", "handle", "name"],
        "phone": ["phone", "phone_number", "phoneNumber", "mobile", "msisdn", "tel", "telephone"],
        "display_name": ["display_name", "displayName", "full_name", "fullname", "name"],
        "bio": ["bio", "about", "description"],
        "avatar_url": ["avatar_url", "avatar", "photo_url", "image_url", "profile_image", "profile_photo"],
    }

    # set mapped fields if present
    for logical, candidates in field_map.items():
        if logical in payload:
            f = _first_existing_field(User, candidates)
            if f and hasattr(db_user, f):
                setattr(db_user, f, payload[logical])

    # direct 1:1 keys (skip password keys already handled)
    for k, v in payload.items():
        if k in ("password", "new_password", "plain_password"):
            continue
        if hasattr(db_user, k):
            try:
                setattr(db_user, k, v)
            except Exception:
                pass

    db.add(db_user)
    db.commit()
    try:
        db.refresh(db_user)
    except Exception:
        pass
    return db_user

