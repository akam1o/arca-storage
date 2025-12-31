"""
Volume service layer.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from arca_storage.api.models import VolumeCreate, VolumeStatus
from arca_storage.cli.lib.lvm import create_lv, delete_lv, resize_lv
from arca_storage.cli.lib.state import delete_volume as state_delete_volume
from arca_storage.cli.lib.state import list_volumes as state_list_volumes
from arca_storage.cli.lib.state import upsert_volume as state_upsert_volume
from arca_storage.cli.lib.validators import validate_name
from arca_storage.cli.lib.xfs import format_xfs, grow_xfs, mount_xfs, umount_xfs
from arca_storage.cli.lib.config import load_config


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

    cfg = load_config()
    vg_name = cfg.vg_name
    export_dir = cfg.export_dir.rstrip("/")
    mount_path = f"{export_dir}/{volume_data.svm}/{volume_data.name}"
    lv_name = f"vol_{volume_data.svm}_{volume_data.name}"

    # Create LV
    lv_path = create_lv(vg_name, lv_name, volume_data.size_gib, thin=volume_data.thin)

    # Format XFS
    format_xfs(lv_path)

    # Mount
    mount_xfs(lv_path, mount_path)

    record = {
        "name": volume_data.name,
        "svm": volume_data.svm,
        "size_gib": volume_data.size_gib,
        "thin": volume_data.thin,
        "fs_type": volume_data.fs_type,
        "mount_path": mount_path,
        "lv_path": lv_path,
        "lv_name": lv_name,
        "status": VolumeStatus.AVAILABLE.value,
        "created_at": datetime.utcnow(),
    }
    state_upsert_volume(
        {
            **record,
            "created_at": record["created_at"].isoformat(),
        }
    )
    return record


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

    cfg = load_config()
    vg_name = cfg.vg_name
    export_dir = cfg.export_dir.rstrip("/")
    mount_path = f"{export_dir}/{svm}/{name}"
    lv_name = f"vol_{svm}_{name}"

    # Resize LV
    resize_lv(vg_name, lv_name, new_size_gib)

    # Grow XFS
    grow_xfs(mount_path)

    record = {
        "name": name,
        "svm": svm,
        "size_gib": new_size_gib,
        "thin": True,
        "fs_type": "xfs",
        "mount_path": mount_path,
        "lv_path": f"/dev/{vg_name}/{lv_name}",
        "lv_name": lv_name,
        "status": VolumeStatus.AVAILABLE.value,
        "created_at": datetime.utcnow(),
    }
    state_upsert_volume({**record, "created_at": record["created_at"].isoformat()})
    return record


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

    cfg = load_config()
    vg_name = cfg.vg_name
    export_dir = cfg.export_dir.rstrip("/")
    mount_path = f"{export_dir}/{svm}/{name}"
    lv_name = f"vol_{svm}_{name}"

    # Unmount
    umount_xfs(mount_path)

    # Delete LV
    delete_lv(vg_name, lv_name)

    state_delete_volume(svm, name)


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
    items = state_list_volumes(svm=svm, name=name)
    return {"items": items[:limit], "next_cursor": None}
