"""
Snapshot resource model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

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
    lv_created: bool = False
    lv_path: Optional[str] = None
    lv_name: Optional[str] = None
    message: str = ""
    last_reconciled: Optional[datetime] = None


class Snapshot(BaseModel):
    """Complete snapshot resource."""

    metadata: ResourceMeta = Field(default_factory=ResourceMeta)
    spec: SnapshotSpec
    status: SnapshotStatus = Field(default_factory=SnapshotStatus)
