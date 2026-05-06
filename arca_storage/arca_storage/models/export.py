"""
NFS Export resource model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from arca_storage.models.base import Phase, ResourceMeta


class ExportSpec(BaseModel):
    """User-declared desired state for an NFS export."""

    svm: str = Field(..., min_length=1, max_length=64)
    volume: str = Field(..., min_length=1, max_length=64)
    client: str  # CIDR notation
    access: str = "RW"
    root_squash: bool = True
    sec: list[str] = Field(default_factory=lambda: ["sys"])
    path: Optional[str] = None
    pseudo: Optional[str] = None
    owner: str = "api"

    @field_validator("sec")
    @classmethod
    def validate_sec(cls, value: list[str]) -> list[str]:
        allowed = {"sys", "krb5", "krb5i", "krb5p"}
        values = [item.strip().lower() for item in value if item.strip()]
        if not values:
            raise ValueError("sec must contain at least one security type")
        unsupported = [item for item in values if item not in allowed]
        if unsupported:
            raise ValueError(f"Unsupported security types: {unsupported}")
        return values


class ExportStatus(BaseModel):
    """System-managed actual state for an export."""

    phase: Phase = Phase.PENDING
    export_id: Optional[int] = None
    path: Optional[str] = None
    pseudo: Optional[str] = None
    ganesha_configured: bool = False
    service_reloaded: bool = False
    message: str = ""
    last_reconciled: Optional[datetime] = None


class Export(BaseModel):
    """Complete export resource."""

    metadata: ResourceMeta = Field(default_factory=ResourceMeta)
    spec: ExportSpec
    status: ExportStatus = Field(default_factory=ExportStatus)
