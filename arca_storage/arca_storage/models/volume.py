"""
Volume resource model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arca_storage.models.base import Phase, ResourceMeta


class VolumeSpec(BaseModel):
    """User-declared desired state for a volume."""

    name: str = Field(..., min_length=1, max_length=64)
    svm: str = Field(..., min_length=1, max_length=64)
    size_gib: int = Field(..., gt=0)
    thin: bool = True
    fs_type: str = "xfs"

    @field_validator("fs_type")
    @classmethod
    def validate_fs_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized != "xfs":
            raise ValueError("fs_type must be 'xfs'")
        return normalized


class QoSStatus(BaseModel):
    """Persisted QoS settings for a volume."""

    model_config = ConfigDict(extra="forbid")

    svm: Optional[str] = None
    volume: Optional[str] = None
    qos_enabled: bool
    device_id: Optional[str] = None
    cgroup_path: Optional[str] = None
    read_iops: Optional[int] = Field(None, gt=0)
    write_iops: Optional[int] = Field(None, gt=0)
    read_bps: Optional[int] = Field(None, gt=0)
    write_bps: Optional[int] = Field(None, gt=0)


class VolumeStatus(BaseModel):
    """System-managed actual state for a volume."""

    phase: Phase = Phase.PENDING
    lv_created: bool = False
    lv_path: Optional[str] = None
    lv_name: Optional[str] = None
    fs_formatted: bool = False
    mounted: bool = False
    mount_path: Optional[str] = None
    message: str = ""
    last_reconciled: Optional[datetime] = None
    create_owner: Optional[str] = None
    create_lease_expires_at: Optional[datetime] = None
    resize_owner: Optional[str] = None
    resize_lease_expires_at: Optional[datetime] = None
    resize_target_size_gib: Optional[int] = None
    qos: Optional[QoSStatus] = None


class Volume(BaseModel):
    """Complete volume resource."""

    metadata: ResourceMeta = Field(default_factory=ResourceMeta)
    spec: VolumeSpec
    status: VolumeStatus = Field(default_factory=VolumeStatus)
