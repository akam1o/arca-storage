"""
SVM (Storage Virtual Machine) resource model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from arca_storage.models.base import Phase, ResourceMeta


class SVMSpec(BaseModel):
    """User-declared desired state for an SVM."""

    name: str = Field(..., min_length=1, max_length=64)
    vlan_id: Optional[int] = Field(default=None, ge=1, le=4094)
    ip_cidr: str
    gateway: Optional[str] = None
    mtu: int = Field(default=1500, ge=68, le=9000)
    root_volume_size_gib: Optional[int] = Field(default=None, gt=0)
    nfs_versions: list[str] = Field(default_factory=lambda: ["4"])


class SVMStatus(BaseModel):
    """System-managed actual state for an SVM."""

    phase: Phase = Phase.PENDING
    namespace_created: bool = False
    vlan_attached: bool = False
    vlan_ifname: Optional[str] = None
    ganesha_configured: bool = False
    lv_created: bool = False
    fs_formatted: bool = False
    pacemaker_group_created: bool = False
    message: str = ""
    last_reconciled: Optional[datetime] = None
    create_owner: Optional[str] = None
    create_lease_expires_at: Optional[datetime] = None


class SVM(BaseModel):
    """Complete SVM resource."""

    metadata: ResourceMeta = Field(default_factory=ResourceMeta)
    spec: SVMSpec
    status: SVMStatus = Field(default_factory=SVMStatus)
