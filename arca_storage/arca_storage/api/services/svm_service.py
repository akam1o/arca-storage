"""
SVM service layer.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from arca_storage.api.models import SVMCreate, SVMStatus
from arca_storage.cli.lib.ganesha import render_config
from arca_storage.cli.lib.netns import attach_vlan, create_namespace, delete_namespace
from arca_storage.cli.lib.pacemaker import create_group, delete_group
from arca_storage.cli.lib.state import delete_svm as state_delete_svm
from arca_storage.cli.lib.state import list_svms as state_list_svms
from arca_storage.cli.lib.state import upsert_svm as state_upsert_svm
from arca_storage.cli.lib.systemd import stop_unit
from arca_storage.cli.lib.validators import (
    infer_gateway_from_ip_cidr,
    validate_ip_cidr,
    validate_ipv4,
    validate_name,
    validate_vlan,
)
from arca_storage.cli.lib.lvm import create_lv
from arca_storage.cli.lib.xfs import format_xfs
from arca_storage.cli.lib.config import load_config


def create_svm(svm_data: SVMCreate) -> Dict[str, Any]:
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
    if svm_data.gateway is not None:
        validate_ipv4(svm_data.gateway)
    gateway_ip = svm_data.gateway or infer_gateway_from_ip_cidr(svm_data.ip_cidr)

    # Create namespace
    create_namespace(svm_data.name)

    # Attach VLAN
    cfg = load_config()
    attach_vlan(svm_data.name, cfg.parent_if, svm_data.vlan_id, svm_data.ip_cidr, gateway_ip, svm_data.mtu)

    # Generate ganesha config
    render_config(svm_data.name, [])

    # Optional root LV (for Pacemaker Filesystem resource)
    if svm_data.root_volume_size_gib:
        vg_name = cfg.vg_name
        lv_name = f"vol_{svm_data.name}"
        try:
            lv_path = create_lv(
                vg_name,
                lv_name,
                svm_data.root_volume_size_gib,
                thin=True,
                thinpool_name=cfg.thinpool_name,
            )
            format_xfs(lv_path)
        except Exception as e:
            if "already exists" not in str(e).lower():
                raise

    export_dir = cfg.export_dir.rstrip("/")

    # Create Pacemaker resource group
    create_group(
        svm_data.name,
        f"{export_dir}/{svm_data.name}",
        vlan_id=svm_data.vlan_id,
        ip=ip_addr,
        prefix=prefix,
        gw=gateway_ip,
        mtu=svm_data.mtu,
        parent_if=cfg.parent_if,
        vg_name=cfg.vg_name,
        drbd_resource_name=cfg.drbd_resource,
        create_filesystem=bool(svm_data.root_volume_size_gib),
    )

    state_upsert_svm(
        {
            "name": svm_data.name,
            "vlan_id": svm_data.vlan_id,
            "ip_cidr": svm_data.ip_cidr,
            "gateway": gateway_ip,
            "mtu": svm_data.mtu,
            "namespace": svm_data.name,
            "vip": ip_addr,
            "status": SVMStatus.AVAILABLE.value,
        }
    )

    # Build response
    return {
        "name": svm_data.name,
        "vlan_id": svm_data.vlan_id,
        "ip_cidr": svm_data.ip_cidr,
        "gateway": gateway_ip,
        "mtu": svm_data.mtu,
        "namespace": svm_data.name,
        "vip": ip_addr,
        "status": SVMStatus.AVAILABLE.value,
        "created_at": datetime.utcnow(),
    }


def list_svms(name: Optional[str] = None, limit: int = 100, cursor: Optional[str] = None) -> Dict[str, Any]:
    """
    List SVMs.

    Args:
        name: Filter by name
        limit: Maximum results
        cursor: Pagination cursor

    Returns:
        Dictionary with items and next_cursor
    """
    items = state_list_svms(name=name)
    return {"items": items[:limit], "next_cursor": None}


def delete_svm(name: str, force: bool = False, delete_volumes: bool = False) -> None:
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

    state_delete_svm(name)
