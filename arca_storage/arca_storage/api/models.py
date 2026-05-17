"""
Pydantic models for API requests and responses.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional, Union

from pydantic import BaseModel, Field, field_validator

from arca_storage.cli.lib.validators import (
    normalize_nfs_client_cidr,
    validate_name as validate_resource_name,
    validate_svm_ip_cidr,
)


def _validate_resource_name(value: str) -> str:
    validate_resource_name(value)
    return value


class SVMStatus(str, Enum):
    """SVM status values."""

    PENDING = "Pending"
    CREATING = "Creating"
    READY = "Ready"
    DELETING = "Deleting"
    FAILED = "Failed"


class VolumeStatus(str, Enum):
    """Volume status values."""

    PENDING = "Pending"
    CREATING = "Creating"
    READY = "Ready"
    DELETING = "Deleting"
    FAILED = "Failed"


class SnapshotStatus(str, Enum):
    """Snapshot status values."""

    PENDING = "Pending"
    CREATING = "Creating"
    READY = "Ready"
    DELETING = "Deleting"
    FAILED = "Failed"


class ExportStatus(str, Enum):
    """Export status values."""

    PENDING = "Pending"
    CREATING = "Creating"
    READY = "Ready"
    DELETING = "Deleting"
    FAILED = "Failed"


# SVM Models


class SVMCreate(BaseModel):
    """Request model for creating an SVM."""

    name: str = Field(..., description="SVM name", min_length=1, max_length=64)
    vlan_id: Optional[int] = Field(None, description="Optional VLAN ID", ge=1, le=4094)
    ip_cidr: str = Field(..., description="IP address with CIDR (e.g., 192.168.10.5/24)")
    gateway: Optional[str] = Field(None, description="Gateway IP (optional; inferred if omitted)")
    mtu: int = Field(1500, description="MTU size", ge=68, le=9000)
    root_volume_size_gib: Optional[int] = Field(
        None, description="Optional root LV size in GiB (creates /dev/<vg>/vol_<svm>)", gt=0
    )

    @field_validator("name")
    def validate_name(cls, v: str) -> str:
        return _validate_resource_name(v)

    @field_validator("ip_cidr")
    def validate_ip_cidr(cls, v: str) -> str:
        validate_svm_ip_cidr(v)
        return v

    @field_validator("gateway")
    def validate_gateway(cls, v: Optional[str]) -> Optional[str]:
        import ipaddress

        if v is None:
            return v
        try:
            ipaddress.IPv4Address(v)
        except Exception as e:
            raise ValueError(f"Invalid gateway IP: {e}")
        return v


class SVM(BaseModel):
    """SVM response model."""

    name: str
    vlan_id: Optional[int] = None
    ip_cidr: str
    gateway: Optional[str] = None
    mtu: int
    namespace: str
    vip: str
    export_root: Optional[str] = None
    status: SVMStatus
    state: Optional[SVMStatus] = None
    created_at: datetime


class SVMData(BaseModel):
    """Nested SVM data envelope."""

    svm: SVM


class SVMResponse(BaseModel):
    """Response model for SVM operations."""

    request_id: str
    status: str
    data: Union[SVMData, SVM]


class SVMListData(BaseModel):
    """SVM list data envelope."""

    items: List[SVM]
    next_cursor: Optional[str] = None


class SVMListResponse(BaseModel):
    """Response model for listing SVMs."""

    request_id: str
    status: str
    data: SVMListData


# CSI compatibility models


class DirectoryCreate(BaseModel):
    """Request model for CSI directory-backed volume creation."""

    svm_name: str = Field(..., description="SVM name", min_length=1, max_length=64)
    path: str = Field(..., description="Directory path relative to SVM root", min_length=1, max_length=64)
    quota_bytes: Optional[int] = Field(None, description="Optional quota/capacity in bytes", gt=0)

    @field_validator("svm_name", "path")
    def validate_name(cls, v: str) -> str:
        return _validate_resource_name(v)


class QuotaSet(BaseModel):
    """Request model for CSI quota/capacity updates."""

    svm_name: str = Field(..., description="SVM name", min_length=1, max_length=64)
    path: str = Field(..., description="Directory path relative to SVM root", min_length=1, max_length=64)
    quota_bytes: int = Field(..., description="Quota/capacity in bytes", gt=0)

    @field_validator("svm_name", "path")
    def validate_name(cls, v: str) -> str:
        return _validate_resource_name(v)


class QuotaExpand(BaseModel):
    """Request model for CSI quota expansion."""

    svm_name: str = Field(..., description="SVM name", min_length=1, max_length=64)
    path: str = Field(..., description="Directory path relative to SVM root", min_length=1, max_length=64)
    new_quota_bytes: int = Field(..., description="New quota/capacity in bytes", gt=0)

    @field_validator("svm_name", "path")
    def validate_name(cls, v: str) -> str:
        return _validate_resource_name(v)


# Volume Models


class VolumeCreate(BaseModel):
    """Request model for creating a volume."""

    name: str = Field(..., description="Volume name", min_length=1, max_length=64)
    svm: str = Field(..., description="SVM name", min_length=1, max_length=64)
    size_gib: int = Field(..., description="Size in GiB", gt=0)
    thin: bool = Field(True, description="Use thin provisioning")
    fs_type: str = Field("xfs", description="Filesystem type")

    @field_validator("name", "svm")
    def validate_name(cls, v: str) -> str:
        return _validate_resource_name(v)

    @field_validator("fs_type")
    def validate_fs_type(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized != "xfs":
            raise ValueError("fs_type must be 'xfs'")
        return normalized


class VolumeResize(BaseModel):
    """Request model for resizing a volume."""

    svm: str = Field(..., description="SVM name")
    new_size_gib: int = Field(..., description="New size in GiB", gt=0)

    @field_validator("svm")
    def validate_svm(cls, v: str) -> str:
        return _validate_resource_name(v)


class VolumeQoSApply(BaseModel):
    """Request model for applying QoS to a volume."""

    svm: str = Field(..., description="SVM name", min_length=1, max_length=64)
    read_iops: Optional[int] = Field(None, description="Read IOPS limit", gt=0)
    write_iops: Optional[int] = Field(None, description="Write IOPS limit", gt=0)
    read_bps: Optional[int] = Field(None, description="Read bandwidth limit (bytes/sec)", gt=0)
    write_bps: Optional[int] = Field(None, description="Write bandwidth limit (bytes/sec)", gt=0)

    @field_validator("svm")
    def validate_svm(cls, v: str) -> str:
        return _validate_resource_name(v)


class VolumeQoS(BaseModel):
    """QoS settings response model."""

    svm: str
    volume: str
    qos_enabled: bool
    device_id: Optional[str] = None
    cgroup_path: Optional[str] = None
    read_iops: Optional[int] = None
    write_iops: Optional[int] = None
    read_bps: Optional[int] = None
    write_bps: Optional[int] = None


class VolumeQoSResponse(BaseModel):
    """Response model for QoS operations."""

    request_id: str
    status: str
    data: dict


class Volume(BaseModel):
    """Volume response model."""

    name: str
    svm: str
    size_gib: int
    thin: bool
    fs_type: str
    mount_path: Optional[str] = None
    lv_path: Optional[str] = None
    lv_name: Optional[str] = None
    export_path: Optional[str] = None
    status: VolumeStatus
    created_at: datetime


class VolumeData(BaseModel):
    """Nested volume data envelope."""

    volume: Volume


class VolumeResponse(BaseModel):
    """Response model for volume operations."""

    request_id: str
    status: str
    data: VolumeData


class VolumeListData(BaseModel):
    """Volume list data envelope."""

    items: List[Volume]
    next_cursor: Optional[str] = None


class VolumeListResponse(BaseModel):
    """Response model for listing volumes."""

    request_id: str
    status: str
    data: VolumeListData


# Snapshot Models


class SnapshotCreate(BaseModel):
    """Request model for creating a snapshot."""

    name: str = Field(..., description="Snapshot name", min_length=1, max_length=64)
    svm: str = Field(..., description="SVM name", min_length=1, max_length=64)
    volume: str = Field(..., description="Source volume name", min_length=1, max_length=64)

    @field_validator("name", "svm", "volume")
    def validate_name(cls, v: str) -> str:
        return _validate_resource_name(v)


class VolumeCloneCreate(BaseModel):
    """Request model for creating a volume from a snapshot."""

    name: str = Field(..., description="New volume name", min_length=1, max_length=64)
    svm: str = Field(..., description="SVM name", min_length=1, max_length=64)
    snapshot: str = Field(..., description="Source snapshot name", min_length=1, max_length=64)
    size_gib: Optional[int] = Field(None, description="Size in GiB (optional, defaults to snapshot size)", gt=0)

    @field_validator("name", "svm", "snapshot")
    def validate_name(cls, v: str) -> str:
        return _validate_resource_name(v)


class Snapshot(BaseModel):
    """Snapshot response model."""

    name: str
    svm: str
    volume: str
    lv_path: Optional[str] = None
    lv_name: Optional[str] = None
    status: SnapshotStatus
    created_at: datetime


class SnapshotData(BaseModel):
    """Nested snapshot data envelope."""

    snapshot: Snapshot


class SnapshotResponse(BaseModel):
    """Response model for snapshot operations."""

    request_id: str
    status: str
    data: SnapshotData


class SnapshotListData(BaseModel):
    """Snapshot list data envelope."""

    items: List[Snapshot]
    next_cursor: Optional[str] = None


class SnapshotListResponse(BaseModel):
    """Response model for listing snapshots."""

    request_id: str
    status: str
    data: SnapshotListData


# Export Models


class ExportCreate(BaseModel):
    """Request model for creating an export."""

    svm: str = Field(..., description="SVM name")
    volume: str = Field(..., description="Volume name")
    client: str = Field(..., description="Client CIDR (e.g., 10.0.0.0/24)")
    access: str = Field("rw", description="Access type: rw or ro")
    root_squash: bool = Field(True, description="Enable root squash")
    sec: List[str] = Field(default_factory=lambda: ["sys"], description="Security types")

    @field_validator("svm", "volume")
    def validate_name(cls, v: str) -> str:
        return _validate_resource_name(v)

    @field_validator("access")
    def validate_access(cls, v: str) -> str:
        if v not in ["rw", "ro"]:
            raise ValueError("Access must be 'rw' or 'ro'")
        return v

    @field_validator("client")
    def validate_client(cls, v: str) -> str:
        return normalize_nfs_client_cidr(v)

    @field_validator("sec")
    def validate_sec(cls, v: List[str]) -> List[str]:
        allowed = {"sys", "krb5", "krb5i", "krb5p"}
        values = [item.strip().lower() for item in v if item.strip()]
        if not values:
            raise ValueError("sec must contain at least one security type")
        unsupported = [item for item in values if item not in allowed]
        if unsupported:
            raise ValueError(f"Unsupported security types: {unsupported}")
        return values


class Export(BaseModel):
    """Export response model."""

    svm: str
    volume: str
    client: str
    access: str
    root_squash: bool
    sec: List[str]
    pseudo: Optional[str] = None
    export_id: Optional[int] = None
    status: ExportStatus
    created_at: datetime


class ExportData(BaseModel):
    """Nested export data envelope."""

    export: Export


class ExportResponse(BaseModel):
    """Response model for export operations."""

    request_id: str
    status: str
    data: ExportData


class ExportListData(BaseModel):
    """Export list data envelope."""

    items: List[Export]
    next_cursor: Optional[str] = None


class ExportListResponse(BaseModel):
    """Response model for listing exports."""

    request_id: str
    status: str
    data: ExportListData


# Common Models


class SuccessResponse(BaseModel):
    """Generic success response."""

    request_id: str
    status: str
    data: dict


class ErrorResponse(BaseModel):
    """Generic error response."""

    request_id: str
    status: str
    error: dict
