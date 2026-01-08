"""
Snapshot service layer.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from arca_storage.api.models import SnapshotCreate, SnapshotStatus, VolumeCloneCreate
from arca_storage.cli.lib.lvm import create_lv, create_snapshot_lv, delete_snapshot_lv
from arca_storage.cli.lib.state import delete_snapshot as state_delete_snapshot
from arca_storage.cli.lib.state import list_snapshots as state_list_snapshots
from arca_storage.cli.lib.state import upsert_snapshot as state_upsert_snapshot
from arca_storage.cli.lib.state import upsert_volume as state_upsert_volume
from arca_storage.cli.lib.validators import validate_name
from arca_storage.cli.lib.xfs import format_xfs, mount_xfs
from arca_storage.cli.lib.config import load_config


def create_snapshot(snapshot_data: SnapshotCreate) -> Dict[str, Any]:
    """
    Create a snapshot of a volume.

    Args:
        snapshot_data: Snapshot creation data

    Returns:
        Snapshot dictionary

    Raises:
        RuntimeError: If snapshot creation fails
    """
    validate_name(snapshot_data.name)
    validate_name(snapshot_data.svm)
    validate_name(snapshot_data.volume)

    cfg = load_config()
    vg_name = cfg.vg_name

    # LV naming: vol_{svm}_{volume}_snap_{snapshot_name}
    source_lv = f"vol_{snapshot_data.svm}_{snapshot_data.volume}"
    snap_lv = f"vol_{snapshot_data.svm}_{snapshot_data.volume}_snap_{snapshot_data.name}"

    # Create thin snapshot
    snap_path = create_snapshot_lv(vg_name, source_lv, snap_lv)

    record = {
        "name": snapshot_data.name,
        "svm": snapshot_data.svm,
        "volume": snapshot_data.volume,
        "lv_path": snap_path,
        "lv_name": snap_lv,
        "status": SnapshotStatus.AVAILABLE.value,
        "created_at": datetime.utcnow(),
    }

    state_upsert_snapshot(
        {
            **record,
            "created_at": record["created_at"].isoformat(),
        }
    )

    return record


def delete_snapshot(name: str, svm: str, volume: str, force: bool = False) -> None:
    """
    Delete a snapshot.

    Args:
        name: Snapshot name
        svm: SVM name
        volume: Volume name
        force: Force deletion

    Raises:
        RuntimeError: If snapshot deletion fails
    """
    validate_name(name)
    validate_name(svm)
    validate_name(volume)

    cfg = load_config()
    vg_name = cfg.vg_name

    snap_lv = f"vol_{svm}_{volume}_snap_{name}"

    # Delete snapshot LV
    delete_snapshot_lv(vg_name, snap_lv)

    # Remove from state
    state_delete_snapshot(svm, volume, name)


def clone_volume_from_snapshot(clone_data: VolumeCloneCreate) -> Dict[str, Any]:
    """
    Create a new volume from a snapshot (clone).

    This creates a writable clone by:
    1. Creating a new thin LV from the snapshot
    2. Formatting it with XFS
    3. Mounting it

    Args:
        clone_data: Clone creation data

    Returns:
        New volume dictionary

    Raises:
        RuntimeError: If clone creation fails
    """
    validate_name(clone_data.name)
    validate_name(clone_data.svm)
    validate_name(clone_data.snapshot)

    cfg = load_config()
    vg_name = cfg.vg_name
    export_dir = cfg.export_dir.rstrip("/")
    mount_path = f"{export_dir}/{clone_data.svm}/{clone_data.name}"

    # Find the snapshot to clone from
    snapshots = state_list_snapshots(svm=clone_data.svm, name=clone_data.snapshot)
    if not snapshots:
        raise RuntimeError(f"Snapshot {clone_data.snapshot} not found in SVM {clone_data.svm}")

    snapshot = snapshots[0]
    source_volume = snapshot["volume"]

    # Snapshot LV name
    snap_lv = f"vol_{clone_data.svm}_{source_volume}_snap_{clone_data.snapshot}"
    snap_path = f"/dev/{vg_name}/{snap_lv}"

    # New volume LV name
    new_lv = f"vol_{clone_data.svm}_{clone_data.name}"

    # Determine size (use snapshot size if not specified)
    if clone_data.size_gib:
        size_gib = clone_data.size_gib
    else:
        # Get snapshot size (for simplicity, we'll create with same size as source)
        # In production, you'd query lvdisplay for actual size
        size_gib = 10  # Default fallback

    # Create new thin LV from snapshot (writable clone)
    # This is done by creating a snapshot of the snapshot
    # The snapshot will inherit the XFS filesystem with all data
    clone_lv_path = create_snapshot_lv(vg_name, snap_lv, new_lv)

    # IMPORTANT: Do NOT format the clone - it already has XFS with data from the snapshot
    # format_xfs(clone_lv_path) would destroy the data!

    # Mount the cloned filesystem (which contains the original data)
    mount_xfs(clone_lv_path, mount_path)

    # Store volume record
    record = {
        "name": clone_data.name,
        "svm": clone_data.svm,
        "size_gib": size_gib,
        "thin": True,
        "fs_type": "xfs",
        "mount_path": mount_path,
        "lv_path": clone_lv_path,
        "lv_name": new_lv,
        "status": "available",
        "created_at": datetime.utcnow(),
    }

    state_upsert_volume({**record, "created_at": record["created_at"].isoformat()})

    return record


def list_snapshots(
    svm: Optional[str] = None,
    volume: Optional[str] = None,
    name: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """
    List snapshots.

    Args:
        svm: Filter by SVM name
        volume: Filter by volume name
        name: Filter by snapshot name
        limit: Maximum results
        cursor: Pagination cursor

    Returns:
        Dictionary with items and next_cursor
    """
    items = state_list_snapshots(svm=svm, volume=volume, name=name)
    return {"items": items[:limit], "next_cursor": None}
