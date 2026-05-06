"""Helpers for safely leasing interrupted create reservations."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from arca_storage.models.base import Phase

ACTIVE_CREATE_PHASES = {Phase.PENDING.value, Phase.CREATING.value}
CREATE_LEASE_DURATION = timedelta(minutes=15)
CREATE_LEASE_HEARTBEAT_INTERVAL = 30.0

logger = logging.getLogger(__name__)


def new_create_owner() -> str:
    return uuid4().hex


def lease_expiration(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + CREATE_LEASE_DURATION


def assign_create_lease(status: Any, owner: str, now: datetime | None = None) -> None:
    status.phase = Phase.CREATING
    status.create_owner = owner
    status.create_lease_expires_at = lease_expiration(now)


def extend_create_lease(status: Any, owner: str, now: datetime | None = None) -> bool:
    phase = getattr(status, "phase", None)
    phase_value = phase.value if isinstance(phase, Phase) else phase
    if phase_value not in ACTIVE_CREATE_PHASES:
        return False
    status.create_owner = owner
    status.create_lease_expires_at = lease_expiration(now)
    return True


def clear_create_lease(status: Any) -> None:
    status.create_owner = None
    status.create_lease_expires_at = None


def create_lease_expired(record: dict[str, Any], now: datetime | None = None) -> bool:
    status = record.get("status", {})
    if status.get("phase") not in ACTIVE_CREATE_PHASES:
        return False

    raw_timestamp = status.get("create_lease_expires_at")
    if not raw_timestamp:
        return False

    try:
        timestamp = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
    except ValueError:
        return False

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    return timestamp.astimezone(timezone.utc) <= (now or datetime.now(timezone.utc))


@contextmanager
def create_lease_heartbeat(refresh: Any, *, interval: float = CREATE_LEASE_HEARTBEAT_INTERVAL):
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(interval):
            try:
                if refresh() is False:
                    logger.warning("Create lease refresh was rejected")
                    return
            except Exception as e:
                logger.warning("Failed to refresh create lease: %s", e)

    thread = threading.Thread(target=beat, name="arca-create-lease-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)
