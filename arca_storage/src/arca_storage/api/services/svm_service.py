"""
SVM service layer.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from arca_storage.api.models import SVMCreate, SVMStatus
from arca_storage.cli.lib.ganesha import render_config
from arca_storage.cli.lib.netns import attach_vlan, create_namespace, delete_namespace
from arca_storage.cli.lib.pacemaker import create_group, delete_group
from arca_storage.cli.lib.systemd import stop_unit
from arca_storage.cli.lib.validators import (validate_ip_cidr, validate_name,
                                   validate_vlan)


async def create_svm(svm_data: SVMCreate) -> Dict[str, Any]:
    """
    Create a new SVM.

    Args:
        svm_data: SVM creation data

    Returns:
        SVM dictionary
    """
    # Validate inputs
    validate_name(svm_data.name)
    validate_vlan(svm_data.vlan_id)
    ip_addr, prefix = validate_ip_cidr(svm_data.ip_cidr)

    # Create namespace
    create_namespace(svm_data.name)

    # Attach VLAN
    attach_vlan(svm_data.name, "bond0", svm_data.vlan_id, svm_data.ip_cidr, svm_data.gateway, svm_data.mtu)

    # Generate ganesha config
    render_config(svm_data.name, [])

    # Create Pacemaker resource group
    create_group(svm_data.name, f"/exports/{svm_data.name}", svm_data.name, f"nfs-ganesha@{svm_data.name}")

    # Build response
    return {
        "name": svm_data.name,
        "vlan_id": svm_data.vlan_id,
        "ip_cidr": svm_data.ip_cidr,
        "gateway": svm_data.gateway,
        "mtu": svm_data.mtu,
        "namespace": svm_data.name,
        "vip": ip_addr,
        "status": SVMStatus.AVAILABLE.value,
        "created_at": datetime.utcnow(),
    }


async def list_svms(name: Optional[str] = None, limit: int = 100, cursor: Optional[str] = None) -> Dict[str, Any]:
    """
    List SVMs.

    Args:
        name: Filter by name
        limit: Maximum results
        cursor: Pagination cursor

    Returns:
        Dictionary with items and next_cursor
    """
    # TODO: Implement actual listing from state file or Pacemaker
    # For now, return empty list
    return {"items": [], "next_cursor": None}


async def delete_svm(name: str, force: bool = False, delete_volumes: bool = False) -> None:
    """
    Delete an SVM.

    Args:
        name: SVM name
        force: Force deletion
        delete_volumes: Delete volumes as well
    """
    validate_name(name)

    # TODO: Check if volumes exist (unless delete_volumes is True)

    # Stop Pacemaker resources
    delete_group(name)

    # Stop ganesha service
    stop_unit(f"nfs-ganesha@{name}")

    # Delete namespace
    delete_namespace(name)
