from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from sqlalchemy.orm import Session
from backend.db import SessionLocal
from backend.crud.scheduler_crud import get_due_tasks, update_task_status

scheduler = BackgroundScheduler()

def run_due_tasks():
    db: Session = SessionLocal()
    now = datetime.utcnow()
    tasks = get_due_tasks(db, now)

    for task in tasks:
        try:
            # Simulate sending/processing
            print(f"[TASK] Executing {task.type} for user {task.user_id} at {now}")
            print(f"CONTENT: {task.content}")

            update_task_status(db, task_id=task.id, status="sent")
        except Exception as e:
            print(f"[ERROR] Failed to process task {task.id}: {e}")
            update_task_status(db, task_id=task.id, status="failed", retry_count=task.retry_count + 1)

    db.close()

def start_scheduler():
    scheduler.add_job(run_due_tasks, 'interval', seconds=30)  # Run every 30s
    scheduler.start()
