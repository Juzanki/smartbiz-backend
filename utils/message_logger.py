# backend/utils/message_logger.py

from sqlalchemy.orm import Session
from backend.models.message import MessageLog
def log_telegram_message(db: Session, chat_id: str, sender_name: str, message: str):
    """
    Save incoming Telegram message to database.
    """
    log = MessageLog(
        chat_id=chat_id,
        sender_name=sender_name,
        message=message
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log

