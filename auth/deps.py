# backend/auth/deps.py
from __future__ import annotations
from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session
from backend.db import get_db
from backend.models.user import User
import os, jwt

JWT_SECRET = os.getenv("JWT_SECRET", "change-me")  # weka env kwenye Render
JWT_ALG = os.getenv("JWT_ALG", "HS256")

def get_current_user(db: Session = Depends(get_db), token: str | None = None) -> User:
    # NOTE: weka hapa logic yako halisi ya kuchota token (from headers/cookies)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    user = db.query(User).get(int(user_id))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user
