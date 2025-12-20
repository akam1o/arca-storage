"""
Volume service layer.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from arca_storage.api.models import VolumeCreate, VolumeStatus
from arca_storage.cli.lib.lvm import create_lv, delete_lv, resize_lv
from arca_storage.cli.lib.validators import validate_name
from arca_storage.cli.lib.xfs import format_xfs, grow_xfs, mount_xfs, umount_xfs


async def create_volume(volume_data: VolumeCreate) -> Dict[str, Any]:
    """
    Create a new volume.

    Args:
        volume_data: Volume creation data

    Returns:
        Volume dictionary
    """
    validate_name(volume_data.name)
    validate_name(volume_data.svm)

    vg_name = "vg_pool_01"
    mount_path = f"/exports/{volume_data.svm}/{volume_data.name}"

    # Create LV
    lv_path = create_lv(vg_name, volume_data.name, volume_data.size_gib, thin=volume_data.thin)

    # Format XFS
    format_xfs(lv_path)

    # Mount
    mount_xfs(lv_path, mount_path)

    return {
        "name": volume_data.name,
        "svm": volume_data.svm,
        "size_gib": volume_data.size_gib,
        "thin": volume_data.thin,
        "fs_type": volume_data.fs_type,
        "mount_path": mount_path,
        "lv_path": lv_path,
        "status": VolumeStatus.AVAILABLE.value,
        "created_at": datetime.utcnow(),
    }


async def resize_volume(name: str, svm: str, new_size_gib: int) -> Dict[str, Any]:
    """
    Resize a volume.

    Args:
        name: Volume name
        svm: SVM name
        new_size_gib: New size in GiB

    Returns:
        Updated volume dictionary
    """
    validate_name(name)
    validate_name(svm)

    vg_name = "vg_pool_01"
    mount_path = f"/exports/{svm}/{name}"

    # Resize LV
    resize_lv(vg_name, name, new_size_gib)

    # Grow XFS
    grow_xfs(mount_path)

    # TODO: Load actual volume data from state
    return {
        "name": name,
        "svm": svm,
        "size_gib": new_size_gib,
        "thin": True,
        "fs_type": "xfs",
        "mount_path": mount_path,
        "lv_path": f"/dev/{vg_name}/{name}",
        "status": VolumeStatus.AVAILABLE.value,
        "created_at": datetime.utcnow(),
    }


async def delete_volume(name: str, svm: str, force: bool = False) -> None:
    """
    Delete a volume.

    Args:
        name: Volume name
        svm: SVM name
        force: Force deletion
    """
    validate_name(name)
    validate_name(svm)

    vg_name = "vg_pool_01"
    mount_path = f"/exports/{svm}/{name}"

    # Unmount
    umount_xfs(mount_path)

    # Delete LV
    delete_lv(vg_name, name)


async def list_volumes(
    svm: Optional[str] = None, name: Optional[str] = None, limit: int = 100, cursor: Optional[str] = None
) -> Dict[str, Any]:
    """
    List volumes.

    Args:
        svm: Filter by SVM name
        name: Filter by volume name
        limit: Maximum results
        cursor: Pagination cursor

    Returns:
        Dictionary with items and next_cursor
    """
    # TODO: Implement actual listing from state file
    return {"items": [], "next_cursor": None}
