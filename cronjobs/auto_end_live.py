from backend.schemas.user import UserOut
from datetime import datetime, timedelta
from backend.db import SessionLocal
from backend.models.live_stream import LiveStream

def auto_end_inactive_streams():
    db = SessionLocal()
    now = datetime.utcnow()
    timeout = now - timedelta(minutes=15)
    streams = db.query(LiveStream).filter(
        LiveStream.ended_at == None,
        LiveStream.last_active_at < timeout
    ).all()

    for stream in streams:
        stream.ended_at = now
        print(f"[Auto-End] Ended stream ID: {stream.id} due to inactivity.")
    db.commit()
    db.close()

