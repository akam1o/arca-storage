"""Helpers for safely resuming interrupted create reservations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from arca_storage.models.base import Phase

ACTIVE_CREATE_PHASES = {Phase.PENDING.value, Phase.CREATING.value}
STALE_CREATE_RESERVATION_AFTER = timedelta(minutes=5)


def is_stale_create_reservation(record: dict[str, Any]) -> bool:
    """Return whether an in-progress create reservation is old enough to resume."""
    raw_timestamp = record.get("updated_at") or record.get("created_at")
    if not raw_timestamp:
        return False

    try:
        timestamp = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
    except ValueError:
        return False

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    age = datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)
    return age >= STALE_CREATE_RESERVATION_AFTER
