from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.webhook import WebhookEndpoint, WebhookDeliveryLog
from backend.schemas.webhook import WebhookEndpointCreate
from datetime import datetime

def create_webhook(db: Session, data: WebhookEndpointCreate):
    webhook = WebhookEndpoint(
        user_id=data.user_id,
        url=data.url,
        secret=data.secret,
        is_active=data.is_active,
        created_at=datetime.utcnow()
    )
    db.add(webhook)
    db.commit()
    db.refresh(webhook)
    return webhook

def get_active_webhooks_by_user(db: Session, user_id: int):
    return db.query(WebhookEndpoint).filter(WebhookEndpoint.user_id == user_id, WebhookEndpoint.is_active == True).all()

def log_delivery(
    db: Session,
    endpoint_id: int,
    payload: str,
    response_code: int,
    success: bool,
    error_message: str | None = None,
    attempts: int = 1
):
    log = WebhookDeliveryLog(
        endpoint_id=endpoint_id,
        payload=payload,
        response_code=response_code,
        success=success,
        error_message=error_message,
        attempts=attempts,
        delivered_at=datetime.utcnow()
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log

def get_webhook_logs(db: Session, endpoint_id: int):
    return db.query(WebhookDeliveryLog).filter(WebhookDeliveryLog.endpoint_id == endpoint_id).order_by(WebhookDeliveryLog.delivered_at.desc()).all()

