from sqlalchemy.orm import Session
from backend.schemas.notification import NotificationCreate
from backend.crud.notification_crud import create_notification

def send_notification(
    db: Session,
    user_id: int,
    title: str,
    message: str,
    notif_type: str = "info"
):
    notif = NotificationCreate(
        user_id=user_id,
        title=title,
        message=message,
        type=notif_type
    )
    return create_notification(db=db, notif=notif)
