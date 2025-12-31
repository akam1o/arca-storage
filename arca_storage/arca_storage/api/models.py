"""
Pydantic models for API requests and responses.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class SVMStatus(str, Enum):
    """SVM status values."""

    CREATING = "creating"
    AVAILABLE = "available"
    DELETING = "deleting"
    ERROR = "error"


class VolumeStatus(str, Enum):
    """Volume status values."""

    CREATING = "creating"
    AVAILABLE = "available"
    RESIZING = "resizing"
    DELETING = "deleting"
    ERROR = "error"


class ExportStatus(str, Enum):
    """Export status values."""

    AVAILABLE = "available"
    ERROR = "error"


# SVM Models


class SVMCreate(BaseModel):
    """Request model for creating an SVM."""

    name: str = Field(..., description="SVM name", min_length=1, max_length=64)
    vlan_id: int = Field(..., description="VLAN ID", ge=1, le=4094)
    ip_cidr: str = Field(..., description="IP address with CIDR (e.g., 192.168.10.5/24)")
    gateway: Optional[str] = Field(None, description="Gateway IP (optional; inferred if omitted)")
    mtu: int = Field(1500, description="MTU size", ge=68, le=9000)
    root_volume_size_gib: Optional[int] = Field(
        None, description="Optional root LV size in GiB (creates /dev/<vg>/vol_<svm>)", gt=0
    )

    @field_validator("name")
    def validate_name(cls, v: str) -> str:
        import re

        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", v):
            raise ValueError(
                "Name must start with alphanumeric and contain only alphanumeric, dots, underscores, or hyphens"
            )
        return v

    @field_validator("ip_cidr")
    def validate_ip_cidr(cls, v: str) -> str:
        import ipaddress

        try:
            parts = v.split("/")
            if len(parts) != 2:
                raise ValueError("CIDR must be in format IP/PREFIX")
            ipaddress.IPv4Address(parts[0])
            prefix = int(parts[1])
            if prefix < 0 or prefix > 32:
                raise ValueError("Prefix must be between 0 and 32")
        except Exception as e:
            raise ValueError(f"Invalid CIDR format: {e}")
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
    vlan_id: int
    ip_cidr: str
    gateway: Optional[str]
    mtu: int
    namespace: str
    vip: str
    status: SVMStatus
    created_at: datetime


class SVMResponse(BaseModel):
    """Response model for SVM operations."""

    request_id: str
    status: str
    data: dict


class SVMListResponse(BaseModel):
    """Response model for listing SVMs."""

    request_id: str
    status: str
    data: dict


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
        import re

        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", v):
            raise ValueError(
                "Name must start with alphanumeric and contain only alphanumeric, dots, underscores, or hyphens"
            )
        return v


class VolumeResize(BaseModel):
    """Request model for resizing a volume."""

    svm: str = Field(..., description="SVM name")
    new_size_gib: int = Field(..., description="New size in GiB", gt=0)


class Volume(BaseModel):
    """Volume response model."""

    name: str
    svm: str
    size_gib: int
    thin: bool
    fs_type: str
    mount_path: str
    lv_path: str
    status: VolumeStatus
    created_at: datetime


class VolumeResponse(BaseModel):
    """Response model for volume operations."""

    request_id: str
    status: str
    data: dict


class VolumeListResponse(BaseModel):
    """Response model for listing volumes."""

    request_id: str
    status: str
    data: dict


# Export Models


class ExportCreate(BaseModel):
    """Request model for creating an export."""

    svm: str = Field(..., description="SVM name")
    volume: str = Field(..., description="Volume name")
    client: str = Field(..., description="Client CIDR (e.g., 10.0.0.0/24)")
    access: str = Field("rw", description="Access type: rw or ro")
    root_squash: bool = Field(True, description="Enable root squash")
    sec: List[str] = Field(["sys"], description="Security types")

    @field_validator("access")
    def validate_access(cls, v: str) -> str:
        if v not in ["rw", "ro"]:
            raise ValueError("Access must be 'rw' or 'ro'")
        return v

    @field_validator("client")
    def validate_client(cls, v: str) -> str:
        import ipaddress

        try:
            parts = v.split("/")
            if len(parts) != 2:
                raise ValueError("CIDR must be in format IP/PREFIX")
            ipaddress.IPv4Network(v, strict=False)
        except Exception as e:
            raise ValueError(f"Invalid CIDR format: {e}")
        return v


class Export(BaseModel):
    """Export response model."""

    svm: str
    volume: str
    client: str
    access: str
    root_squash: bool
    sec: List[str]
    pseudo: str
    export_id: int
    status: ExportStatus
    created_at: datetime


class ExportResponse(BaseModel):
    """Response model for export operations."""

    request_id: str
    status: str
    data: dict


class ExportListResponse(BaseModel):
    """Response model for listing exports."""

    request_id: str
    status: str
    data: dict


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
