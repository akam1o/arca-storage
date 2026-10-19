"""
Snapshot resource model.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from arca_storage.models.base import Phase, ResourceMeta


class SnapshotSpec(BaseModel):
    """User-declared desired state for a snapshot."""

    name: str = Field(..., min_length=1, max_length=64)
    svm: str = Field(..., min_length=1, max_length=64)
    volume: str = Field(..., min_length=1, max_length=64)


class SnapshotStatus(BaseModel):
    """System-managed actual state for a snapshot."""

    phase: Phase = Phase.PENDING
    size_gib: int | None = None
    lv_created: bool = False
    lv_path: str | None = None
    lv_name: str | None = None
    message: str = ""
    last_reconciled: datetime | None = None
    create_owner: str | None = None
    create_lease_expires_at: datetime | None = None


class Snapshot(BaseModel):
    """Complete snapshot resource."""

    metadata: ResourceMeta = Field(default_factory=ResourceMeta)
    spec: SnapshotSpec
    status: SnapshotStatus = Field(default_factory=SnapshotStatus)
