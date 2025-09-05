# backend/services/platform_status.py
from __future__ import annotations

import logging
from typing import Iterable, Mapping, Any, Optional

from sqlalchemy.orm import Session
from sqlalchemy import select

from backend.models.platform_status import PlatformStatus, PlatformKind, ConnectionState

log = logging.getLogger(__name__)

def normalize_platform(platform: str | PlatformKind) -> PlatformKind:
    try:
        return platform if isinstance(platform, PlatformKind) else PlatformKind(platform.lower())
    except Exception:
        return PlatformKind.other

def get_or_create_platform_status(
    session: Session, *, user_id: int, platform: str | PlatformKind
) -> PlatformStatus:
    plat = normalize_platform(platform)
    stmt = select(PlatformStatus).where(
        PlatformStatus.user_id == user_id,
        PlatformStatus.platform == plat,
    ).limit(1)
    obj = session.execute(stmt).scalar_one_or_none()
    if obj:
        return obj
    obj = PlatformStatus(user_id=user_id, platform=plat)
    session.add(obj)
    session.flush()  # pata id mapema
    return obj

def update_platform_status(
    session: Session,
    *,
    user_id: int,
    platform: str | PlatformKind,
    is_connected: Optional[bool] = None,
    state: Optional[ConnectionState] = None,
    token_expiry: Optional[dt.datetime] = None,
    note: Optional[str] = None,
    error_code: Optional[str] = None,
    error_detail: Optional[str] = None,
    commit: bool = True,
) -> PlatformStatus:
    ps = get_or_create_platform_status(session, user_id=user_id, platform=platform)
    # Chagua precedence: state > is_connected
    if state is not None:
        if state == ConnectionState.connected:
            ps.mark_connected(token_expiry=token_expiry, note=note)
        elif state == ConnectionState.disconnected:
            ps.mark_disconnected(note=note)
        elif state == ConnectionState.expired:
            ps.mark_expired(note=note)
        elif state == ConnectionState.error:
            ps.mark_error(code=error_code, detail=error_detail, note=note)
        else:
            ps.state = state
    elif is_connected is not None:
        if is_connected:
            ps.mark_connected(token_expiry=token_expiry, note=note)
        else:
            ps.mark_disconnected(note=note)

    # Ikiwa hakuna state/flag, weka tu taarifa za ziada
    if token_expiry:
        ps.access_token_expiry = token_expiry
    if note and not state and is_connected is None:
        ps.status_note = note
    if error_code or error_detail:
        ps.error_code = error_code
        ps.error_detail = error_detail

    ps.bump_check()

    if commit:
        session.commit()
    return ps

def send_scheduled_posts(
    *,
    messages: Iterable[Mapping[str, Any]],
    send_message_to_platform: Callable[[Mapping[str, Any]], None],
) -> None:
    """
    Tuma ujumbe mbalimbali. Huruka ujumbe batili badala ya kusitisha batch nzima.
    Inatarajia kila message iwe na: platform, recipient, text/payload n.k.
    """
    for i, message in enumerate(messages, start=1):
        platform = message.get("platform")
        recipient = message.get("recipient")
        if not platform or not recipient:
            log.warning("Skipping invalid message at idx=%s: %s", i, message)
            continue  # ✅ usi-stop batch nzima
        try:
            send_message_to_platform(message)
        except Exception as exc:  # pragma: no cover
            log.exception("Failed to send message idx=%s to %s: %s", i, platform, exc)
            continue
