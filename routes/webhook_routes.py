from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.schemas.webhook import WebhookEndpointCreate, WebhookEndpointOut, TaskFailureLogOut
from backend.crud import webhook_crud

router = APIRouter(
    prefix="/webhooks",
    tags=["Webhooks"]
)

@router.post("/", response_model=WebhookEndpointOut)
def create_webhook(
    data: WebhookEndpointCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if data.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Unauthorized")
    return webhook_crud.create_webhook(db, data)

@router.get("/", response_model=List[WebhookEndpointOut])
def get_my_webhooks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return webhook_crud.get_active_webhooks_by_user(db, current_user.id)

@router.get("/{endpoint_id}/logs", response_model=List[TaskFailureLogOut])
def get_webhook_logs(
    endpoint_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    logs = webhook_crud.get_webhook_logs(db, endpoint_id)
    return logs
