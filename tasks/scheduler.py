# backend/tasks/scheduler.py
# -*- coding: utf-8 -*-
"""
Lightweight, production-safe background scheduler for sending queued messages.

Design goals:
- Non-blocking background task (never blocks Uvicorn workers).
- Small, jittered polling interval; configurable via env.
- Safe retries with exponential backoff and jitter.
- Idempotent CRUD updates; never crash if optional pieces are missing.
- Clean startup/shutdown with graceful cancellation.
- Uses a bridge layer so the loop works even if your concrete CRUD isn't ready yet.
"""

from __future__ import annotations

import os
import asyncio
import random
import logging
import inspect
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Dict, Any, Optional, Iterable

from sqlalchemy.orm import Session

from backend.db import SessionLocal
# 👇 Use the bridge so missing CRUD functions never crash the loop.
from backend.crud import scheduler_bridge as crud

# Optional sender utilities (swap with your own providers)
from backend.utils.telegram_bot import send_telegram_message
from backend.utils.whatsapp import send_whatsapp_message
from backend.utils.sms import send_sms_message

log = logging.getLogger("smartbiz.scheduler")

# === Internal state ===
_bg_task: Optional[asyncio.Task] = None
_stop_event: Optional[asyncio.Event] = None
_running: bool = False

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

# Map platforms to callables.
# If your senders are async, we auto-detect and await; if sync, we run in a thread.
SENDERS: Dict[str, Callable[..., Any]] = {
    "telegram": send_telegram_message,
    "whatsapp": send_whatsapp_message,
    "sms": send_sms_message,
}


@contextmanager
def db_session() -> Iterable[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def _maybe_call_sender(sender: Callable[..., Any], to: str, text: str) -> None:
    """Call either async or sync sender without blocking the loop."""
    if inspect.iscoroutinefunction(sender):
        await sender(to, text)  # type: ignore[func-returns-value]
    else:
        await asyncio.to_thread(sender, to, text)  # type: ignore[arg-type]


async def _send_with_retries(platform: str, to: str, text: str, *, retries: int = 2) -> None:
    """
    Try to send with exponential backoff + jitter.
    Keep delays short (mobile-first / serverless-friendly).
    """
    sender = SENDERS.get(platform)
    if not sender:
        raise ValueError(f"Unsupported platform: {platform!r}")

    delay = 0.8  # seconds; initial
    last_exc: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            await _maybe_call_sender(sender, to, text)
            return
        except Exception as e:  # pragma: no cover
            last_exc = e
            # small jittered backoff so we don't hammer the API on weak networks
            sleep_for = delay + random.uniform(0.0, 0.4)
            log.warning("[Scheduler] send attempt %s failed (%s). Retrying in %.2fs",
                        attempt + 1, type(e).__name__, sleep_for)
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                raise
            delay *= 1.6

    # Exhausted
    assert last_exc is not None
    raise last_exc


async def _process_one_message(db: Session, msg: dict) -> None:
    """
    Process a single message dict returned by the bridge.
    Expected keys: id, channel/platform, payload or (recipient, message).
    Bridge doesn't enforce schema; be defensive.
    """
    message_id = msg.get("id")
    # Flexible payload extraction
    platform = msg.get("platform") or msg.get("channel") or "in_app"
    payload = msg.get("payload") or {}
    to = msg.get("recipient") or payload.get("to") or payload.get("phone") or payload.get("user_id")
    text = msg.get("message") or payload.get("text") or payload.get("body") or ""

    if not to:
        raise ValueError("Missing recipient")
    if not text:
        raise ValueError("Missing message text")

    # Optional: mark as processing (idempotent if your CRUD supports it)
    try:
        # If your CRUD has this function exposed through the bridge, great.
        # Otherwise it's a no-op and that's fine.
        from backend.crud import message_queue_crud as qcrud  # optional
        qcrud.mark_as_processing(db, message_id)
    except Exception:
        pass

    # Send
    await _send_with_retries(platform, str(to), str(text), retries=2)

    # Mark sent (bridge handles missing impl safely)
    crud.mark_message_sent(db, message_id, sent_at=_utcnow())
    crud.log_message_event(db, message_id, "info", "Message dispatched successfully")


async def _tick_once() -> None:
    """
    A single polling tick:
    - Pull due messages via the bridge (returns [] if not implemented yet).
    - Process them sequentially by default (safe for small volumes).
      You can bump to small concurrency with asyncio.gather + semaphore if needed.
    """
    with db_session() as db:
        due = crud.get_due_unsent_messages(db, _utcnow(), limit=100)
        if not due:
            return

        # Optional small concurrency (tunable via env)
        max_conc = max(1, int(os.getenv("SCHEDULER_MAX_CONCURRENCY", "1")))
        sem = asyncio.Semaphore(max_conc)

        async def _run(msg: dict):
            async with sem:
                try:
                    await _process_one_message(db, msg)
                except Exception as e:
                    # Persist failure if your CRUD supports it; otherwise the bridge logs and continues
                    crud.mark_message_failed(db, msg.get("id"), error=str(e))
                    crud.log_message_event(db, msg.get("id"), "error", f"Dispatch failed: {e}")

        # Fire tasks; keep it bounded by the semaphore
        tasks = [asyncio.create_task(_run(m)) for m in due]
        try:
            await asyncio.gather(*tasks, return_exceptions=False)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()


async def _worker_loop(poll_interval: float) -> None:
    """
    Main polling loop. Exits when _stop_event is set or the task is cancelled.
    """
    global _running
    _running = True
    log.info("[Scheduler] started (interval=%.2fs).", poll_interval)
    assert _stop_event is not None
    try:
        while not _stop_event.is_set():
            try:
                await _tick_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover
                # Never crash the loop on a single failure
                log.exception("[Scheduler] unexpected error: %s", e)

            # Sleep with small jitter to avoid thundering herd
            jitter = random.uniform(0.0, 2.0)
            try:
                await asyncio.sleep(poll_interval + jitter)
            except asyncio.CancelledError:
                raise
    finally:
        _running = False
        log.info("[Scheduler] stopped.")


# ----------------------------- Public API ---------------------------------

def is_running() -> bool:
    """Expose running state for health endpoints."""
    return _running


async def trigger_once() -> None:
    """
    Manually trigger one scheduler tick (useful for debugging via an admin endpoint).
    """
    await _tick_once()


async def start_schedulers() -> None:
    """
    Create the background task once. Safe to call multiple times.
    If ENABLE_SCHEDULER=false, this becomes a no-op.
    """
    if os.getenv("ENABLE_SCHEDULER", "true").strip().lower() in {"0", "false", "no", "off"}:
        log.info("[Scheduler] disabled via ENABLE_SCHEDULER.")
        return

    global _bg_task, _stop_event
    if _bg_task and not _bg_task.done():
        log.info("[Scheduler] already running.")
        return

    interval = float(os.getenv("SCHEDULER_TICK_SECONDS", "10"))
    _stop_event = asyncio.Event()
    _bg_task = asyncio.create_task(_worker_loop(interval))


async def stop_schedulers() -> None:
    """
    Gracefully stop the background task on shutdown.
    """
    global _bg_task, _stop_event
    if not _bg_task:
        return

    if _stop_event:
        _stop_event.set()

    _bg_task.cancel()
    try:
        await _bg_task
    except asyncio.CancelledError:
        pass
    finally:
        _bg_task = None
        _stop_event = None
