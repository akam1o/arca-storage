"""
Volume resource model.
"""

from __future__ import annotations

from datetime import datetime

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

    svm: str | None = None
    volume: str | None = None
    qos_enabled: bool
    device_id: str | None = None
    cgroup_path: str | None = None
    read_iops: int | None = Field(None, gt=0)
    write_iops: int | None = Field(None, gt=0)
    read_bps: int | None = Field(None, gt=0)
    write_bps: int | None = Field(None, gt=0)


class VolumeStatus(BaseModel):
    """System-managed actual state for a volume."""

    phase: Phase = Phase.PENDING
    lv_created: bool = False
    lv_path: str | None = None
    lv_name: str | None = None
    fs_formatted: bool = False
    mounted: bool = False
    mount_path: str | None = None
    message: str = ""
    last_reconciled: datetime | None = None
    create_owner: str | None = None
    create_lease_expires_at: datetime | None = None
    resize_owner: str | None = None
    resize_lease_expires_at: datetime | None = None
    resize_target_size_gib: int | None = None
    qos: QoSStatus | None = None


class Volume(BaseModel):
    """Complete volume resource."""

    metadata: ResourceMeta = Field(default_factory=ResourceMeta)
    spec: VolumeSpec
    status: VolumeStatus = Field(default_factory=VolumeStatus)
