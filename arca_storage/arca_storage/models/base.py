"""
Base types shared across all resource models.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, Field


class Phase(str, Enum):
    """Lifecycle phase of a resource."""

    PENDING = "Pending"
    CREATING = "Creating"
    READY = "Ready"
    DELETING = "Deleting"
    FAILED = "Failed"


class ResourceMeta(BaseModel):
    """Metadata common to every resource."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    generation: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def bump(self) -> None:
        self.generation += 1
        self.updated_at = datetime.now(timezone.utc)


def resource_meta_from_record(record: Mapping[str, Any]) -> ResourceMeta:
    """Rebuild resource metadata from a database record."""
    values = {
        "id": record["id"],
        "generation": record.get("generation", 1),
    }
    for field in ("created_at", "updated_at"):
        value = record.get(field)
        if value is not None:
            values[field] = value
    return ResourceMeta(**values)
