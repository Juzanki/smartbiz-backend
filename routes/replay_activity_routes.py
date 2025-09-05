from __future__ import annotations
# backend/routes/replay_activity.py
from typing import Optional, Any, Dict
from contextlib import suppress
from datetime import datetime, timezone

from fastapi import (
    APIRouter, Depends, Header, HTTPException, Query, Request, status
)
from sqlalchemy.orm import Session

from backend.dependencies import get_db, get_current_user_optional
from backend.schemas.replay_activity_schemas import ReplayActivityIn, ReplayActivityOut
# CRUD
from backend.crud import replay_activity_crud as _crud

router = APIRouter(prefix="/replay-activity", tags=["Replay Activity"])

# Optional advanced CRUD funcs (zikipatikana)
LOG_IDEMP = getattr(_crud, "log_activity_idempotent", None)
GET_STATS = getattr(_crud, "get_stats", None)
GET_RECENT = getattr(_crud, "get_recent_for_user", None)

def _utc() -> datetime:
    return datetime.now(timezone.utc)

def _client_meta(request: Request) -> Dict[str, Any]:
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "referer": request.headers.get("referer"),
        "x_forwarded_for": request.headers.get("x-forwarded-for"),
        "ts": int(_utc().timestamp()),
    }

@router.post(
    "/{video_post_id}",
    response_model=ReplayActivityOut,
    status_code=status.HTTP_201_CREATED,
    summary="Log replay activity (idempotent + dedup)"
)
def log_replay_activity(
    video_post_id: int,
    data: ReplayActivityIn,
    request: Request,
    db: Session = Depends(get_db),
    user_id: Optional[int] = Depends(get_current_user_optional),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    dedup_seconds: int = Query(30, ge=0, le=600, description="Zuia duplicate ndani ya muda huu"),
):
    """
    - Tumia `Idempotency-Key` kuepuka kuandika mara mbili request ile ile (mobile retries).
    - `dedup_seconds` huzuia spamming ya action ile ile (user + video + platform + action).
    - Huhifadhi meta ndogo (ip/user_agent/referrer) kwa analytics.
    """
    if video_post_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid video_post_id")

    meta = _client_meta(request)

    # Jaribu advanced CRUD ikiwa ipo
    if LOG_IDEMP:
        return LOG_IDEMP(
            db=db,
            user_id=user_id,
            video_post_id=video_post_id,
            action=data.action,
            platform=data.platform,
            idempotency_key=idempotency_key,
            dedup_seconds=dedup_seconds,
            meta=meta,
        )

    # Fallback: tumia log_activity ya sasa; ikiwa haina idempotency, CRUD yako iangalie ndani
    # (unaweza kuipanua baadaye kutumia idempotency_key & dedup_seconds).
    return _crud.log_activity(
        db, user_id, video_post_id, data.action, data.platform, meta  # type: ignore[arg-type]
    )

# ---------- Extra: Stats ndogo kwa UI (ikisaidia kuepuka queries nzito kwa mobile) ----------

@router.get(
    "/{video_post_id}/stats",
    summary="Stats fupi za replay (counts per action/platform)",
    response_model=dict
)
def replay_stats(
    video_post_id: int,
    db: Session = Depends(get_db),
):
    """
    Inarudisha kitu kama:
    {
      "video_post_id": 123,
      "total": 1240,
      "by_action": {"play": 1000, "pause": 120, "seek": 80, "finish": 40},
      "by_platform": {"web": 900, "android": 250, "ios": 90}
    }
    """
    if GET_STATS:
        return GET_STATS(db, video_post_id)

    # Fallback nyepesi (acha irudishe placeholder hadi uandike CRUD ya kweli)
    return {
        "video_post_id": video_post_id,
        "total": 0,
        "by_action": {},
        "by_platform": {},
    }

@router.get(
    "/me/recent",
    summary="Mikusanyiko ya karibuni ya mtumiaji aliyeingia",
    response_model=list[ReplayActivityOut]
)
def my_recent_replays(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user_id: Optional[int] = Depends(get_current_user_optional),
):
    if not user_id:
        return []
    if GET_RECENT:
        return GET_RECENT(db, user_id=user_id, limit=limit)
    # Fallback placeholder
    return []
