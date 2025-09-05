from __future__ import annotations
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import List, Optional

from backend.db import get_db
from backend.schemas.injection_log import InjectionLogOut
from backend.crud.injection_log import get_logs

router = APIRouter(prefix="/logs", tags=["Injection Logs"])


@router.get(
    "/injection",
    response_model=List[InjectionLogOut],
    summary="📜 View Injection Logs"
)
def read_logs(
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(50, le=100, description="Maximum number of logs to return"),
    tag: Optional[str] = Query(None, description="Filter by tag (e.g., backend:orders)"),
    db: Session = Depends(get_db)
):
    """
    Retrieve a list of SmartInjectGPT injection logs.
    
    Parameters:
    - skip: Number of records to skip for pagination
    - limit: Max number of logs to return (max 100)
    - tag: Filter logs by tag (optional)

    Returns:
    - List of InjectionLogOut entries
    """
    logs = get_logs(db=db, skip=skip, limit=limit, tag=tag)
    return logs
