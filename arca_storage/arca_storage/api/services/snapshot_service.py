"""
Snapshot service layer.

Delegates to the Snapshot reconciler for idempotent, step-tracked operations.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from arca_storage.api.models import SnapshotCreate, VolumeCloneCreate
from arca_storage.context import get_context
from arca_storage.create_resume import ACTIVE_CREATE_PHASES, is_stale_create_reservation
from arca_storage.db import encode_cursor
from arca_storage.errors import AlreadyExistsError, InternalError, InvalidArgumentError, NotFoundError, PreconditionFailedError
from arca_storage.models.base import Phase
from arca_storage.models.snapshot import Snapshot, SnapshotSpec
from arca_storage.models.volume import Volume, VolumeSpec
from arca_storage.cli.lib.validators import validate_name
from arca_storage.api.services.volume_service import build_volume_export_path


def create_snapshot(snapshot_data: SnapshotCreate) -> Dict[str, Any]:
    """Create a snapshot via the reconciler."""
    validate_name(snapshot_data.name)
    validate_name(snapshot_data.svm)
    validate_name(snapshot_data.volume)

    ctx = get_context()
    source_record = ctx.db.get_volume(snapshot_data.svm, snapshot_data.volume)
    if not source_record:
        raise NotFoundError("Volume", f"{snapshot_data.svm}/{snapshot_data.volume}")
    if not bool(source_record.get("spec", {}).get("thin", True)):
        raise PreconditionFailedError(
            f"Volume '{snapshot_data.svm}/{snapshot_data.volume}' is not thin-provisioned; snapshots require thin volumes",
            {
                "resource": "Volume",
                "name": f"{snapshot_data.svm}/{snapshot_data.volume}",
                "thin": False,
            },
        )
    requested_spec = SnapshotSpec(
        name=snapshot_data.name,
        svm=snapshot_data.svm,
        volume=snapshot_data.volume,
    )
    snapshot = Snapshot(spec=requested_spec)
    try:
        ctx.db.insert_snapshot(snapshot)
    except AlreadyExistsError:
        existing = ctx.db.list_snapshots(
            svm=snapshot_data.svm,
            volume=snapshot_data.volume,
            name=snapshot_data.name,
            limit=1,
        )
        record = existing[0] if existing else None
        if _can_resume_create(record, requested_spec):
            return _resume_snapshot_create(ctx, record)
        raise AlreadyExistsError("Snapshot", f"{snapshot_data.svm}/{snapshot_data.volume}/{snapshot_data.name}")

    snapshot = ctx.snapshot_reconciler.reconcile(snapshot)

    if snapshot.status.phase == Phase.FAILED:
        raise RuntimeError(snapshot.status.message)

    return _snapshot_to_dict(snapshot)


def delete_snapshot(name: str, svm: str, volume: str, force: bool = False) -> None:
    """Delete a snapshot via the reconciler."""
    validate_name(name)
    validate_name(svm)
    validate_name(volume)

    ctx = get_context()
    records = ctx.db.list_snapshots(svm=svm, volume=volume, name=name)
    if not records:
        raise NotFoundError("Snapshot", f"{svm}/{volume}/{name}")

    record = records[0]
    snapshot = Snapshot(
        metadata=_meta_from_record(record),
        spec=SnapshotSpec.model_validate(record["spec"]),
        status=_parse_status(record),
    )
    snapshot.status.phase = Phase.DELETING
    result = ctx.snapshot_reconciler.reconcile(snapshot)
    if result.status.phase == Phase.FAILED:
        raise InternalError(
            result.status.message or f"Failed to delete Snapshot '{svm}/{volume}/{name}'",
            {"resource": "Snapshot", "name": f"{svm}/{volume}/{name}"},
        )


def clone_volume_from_snapshot(source_volume: str, clone_data: VolumeCloneCreate) -> Dict[str, Any]:
    """Create a new volume from a snapshot (clone)."""
    validate_name(source_volume)
    validate_name(clone_data.name)
    validate_name(clone_data.svm)
    validate_name(clone_data.snapshot)

    ctx = get_context()
    if ctx.db.get_volume(clone_data.svm, clone_data.name):
        raise AlreadyExistsError("Volume", f"{clone_data.svm}/{clone_data.name}")
    cfg = ctx.settings.to_reconciler_config()
    vg_name = cfg["vg_name"]
    export_dir = cfg["export_dir"]

    # Find the snapshot on the source volume from the route path.
    snapshots = ctx.db.list_snapshots(svm=clone_data.svm, volume=source_volume, name=clone_data.snapshot)
    if not snapshots:
        raise NotFoundError("Snapshot", f"{clone_data.svm}/{source_volume}/{clone_data.snapshot}")

    snap_record = snapshots[0]
    source_record = ctx.db.get_volume(clone_data.svm, source_volume)
    source_size_gib = int(source_record.get("spec", {}).get("size_gib") or 10) if source_record else 10
    target_size_gib = max(clone_data.size_gib or source_size_gib, source_size_gib)
    snap_lv = f"vol_{clone_data.svm}_{source_volume}_snap_{clone_data.snapshot}"
    new_lv = f"vol_{clone_data.svm}_{clone_data.name}"
    mount_path = f"{export_dir}/{clone_data.svm}/{clone_data.name}"

    clone_lv_path = ctx.adapters.lvm.create_snapshot(vg_name, snap_lv, new_lv)
    mounted = False
    try:
        ctx.adapters.xfs.mount(clone_lv_path, mount_path, extra_options=["nouuid"])
        mounted = True
        if target_size_gib > source_size_gib:
            ctx.adapters.lvm.resize_lv(vg_name, new_lv, target_size_gib)
            ctx.adapters.xfs.grow(mount_path)
    except Exception:
        if mounted:
            try:
                ctx.adapters.xfs.umount(mount_path)
            except Exception:
                pass
        try:
            ctx.adapters.lvm.delete_lv(vg_name, new_lv)
        except Exception:
            pass
        raise

    # Store as volume
    volume = Volume(
        spec=VolumeSpec(
            name=clone_data.name,
            svm=clone_data.svm,
            size_gib=target_size_gib,
            thin=True,
        ),
    )
    volume.status.phase = Phase.READY
    volume.status.lv_created = True
    volume.status.fs_formatted = True
    volume.status.mounted = True
    volume.status.lv_path = clone_lv_path
    volume.status.lv_name = new_lv
    volume.status.mount_path = mount_path
    ctx.db.upsert_volume(volume)

    return {
        "name": clone_data.name,
        "svm": clone_data.svm,
        "size_gib": target_size_gib,
        "status": Phase.READY.value,
        "lv_path": clone_lv_path,
        "mount_path": mount_path,
        "export_path": build_volume_export_path(ctx, clone_data.svm, mount_path),
        "created_at": volume.metadata.created_at,
    }


def list_snapshots(
    svm: Optional[str] = None,
    volume: Optional[str] = None,
    name: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """List snapshots from the database."""
    ctx = get_context()
    try:
        records = ctx.db.list_snapshots(svm=svm, volume=volume, name=name, limit=limit + 1, cursor=cursor)
    except ValueError as e:
        raise InvalidArgumentError(str(e), {"cursor": cursor}) from e
    next_cursor = None
    if len(records) > limit:
        spec = records[limit - 1]["spec"]
        next_cursor = encode_cursor([spec["svm"], spec["volume"], spec["name"]])
        records = records[:limit]
    items = [_snapshot_record_to_dict(record) for record in records]
    return {"items": items, "next_cursor": next_cursor}


def _snapshot_to_dict(snap: Snapshot) -> Dict[str, Any]:
    return {
        "name": snap.spec.name,
        "svm": snap.spec.svm,
        "volume": snap.spec.volume,
        "lv_path": snap.status.lv_path,
        "lv_name": snap.status.lv_name,
        "status": snap.status.phase.value,
        "created_at": snap.metadata.created_at,
    }


def _snapshot_record_to_dict(record: Dict[str, Any]) -> Dict[str, Any]:
    spec = record.get("spec", {})
    status = record.get("status", {})
    return {
        "name": spec.get("name"),
        "svm": spec.get("svm"),
        "volume": spec.get("volume"),
        "lv_path": status.get("lv_path"),
        "lv_name": status.get("lv_name"),
        "status": status.get("phase"),
        "created_at": record.get("created_at"),
    }


def _meta_from_record(record: Dict[str, Any]) -> Any:
    from arca_storage.models.base import ResourceMeta
    return ResourceMeta(id=record["id"], generation=record.get("generation", 1))


def _parse_status(record: Dict[str, Any]) -> Any:
    from arca_storage.models.snapshot import SnapshotStatus
    return SnapshotStatus.model_validate(record["status"])


def _can_resume_create(record: Dict[str, Any], requested_spec: SnapshotSpec) -> bool:
    if not record:
        return False
    status = record.get("status", {})
    phase = status.get("phase")
    if SnapshotSpec.model_validate(record["spec"]) != requested_spec:
        return False
    if phase in ACTIVE_CREATE_PHASES:
        return is_stale_create_reservation(record)
    if phase != Phase.FAILED.value:
        return False
    if _is_failed_delete(status):
        return False
    return not status.get("lv_created", False)


def _is_failed_delete(status: Dict[str, Any]) -> bool:
    return str(status.get("message") or "").startswith("Delete failed:")


def _resume_snapshot_create(ctx: Any, record: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = Snapshot(
        metadata=_meta_from_record(record),
        spec=SnapshotSpec.model_validate(record["spec"]),
        status=_parse_status(record),
    )
    snapshot.status.phase = Phase.CREATING
    snapshot.status.message = ""
    snapshot = ctx.snapshot_reconciler.reconcile(snapshot)
    if snapshot.status.phase == Phase.FAILED:
        raise RuntimeError(snapshot.status.message)
    return _snapshot_to_dict(snapshot)
