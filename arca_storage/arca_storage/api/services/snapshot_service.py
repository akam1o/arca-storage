"""
Snapshot service layer.

Delegates to the Snapshot reconciler for idempotent, step-tracked operations.
"""

from __future__ import annotations

from math import ceil
from typing import Any, Dict, Optional

from arca_storage.api.models import SnapshotCreate, VolumeCloneCreate
from arca_storage.context import get_context
from arca_storage.create_resume import (
    ACTIVE_CREATE_PHASES,
    assign_create_lease,
    clear_create_lease,
    create_lease_heartbeat,
    extend_create_lease,
    new_create_owner,
)
from arca_storage.db import encode_cursor
from arca_storage.errors import (
    AlreadyExistsError,
    CreateLeaseLostError,
    InternalError,
    InvalidArgumentError,
    NotFoundError,
    PreconditionFailedError,
)
from arca_storage.models.base import Phase, resource_meta_from_record
from arca_storage.models.snapshot import Snapshot, SnapshotSpec
from arca_storage.models.volume import Volume, VolumeSpec
from arca_storage.cli.lib.validators import snapshot_lv_name, validate_name, volume_lv_name
from arca_storage.api.services.volume_service import build_volume_export_path, require_volume_ready_record
from arca_storage.reconcilers.lvm_resume import create_snapshot_lv_or_accept_existing


def create_snapshot(snapshot_data: SnapshotCreate) -> Dict[str, Any]:
    """Create a snapshot via the reconciler."""
    validate_name(snapshot_data.name)
    validate_name(snapshot_data.svm)
    validate_name(snapshot_data.volume)
    snapshot_lv_name(snapshot_data.svm, snapshot_data.volume, snapshot_data.name)

    ctx = get_context()
    source_record = ctx.db.get_volume(snapshot_data.svm, snapshot_data.volume)
    if not source_record:
        raise NotFoundError("Volume", f"{snapshot_data.svm}/{snapshot_data.volume}")
    require_volume_ready_record(source_record, snapshot_data.svm, snapshot_data.volume)
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
    snapshot.status.size_gib = int(source_record.get("spec", {}).get("size_gib") or 10)
    owner = new_create_owner()
    assign_create_lease(snapshot.status, owner)
    try:
        ctx.db.insert_snapshot(snapshot, require_ready_volume=True)
    except AlreadyExistsError:
        existing = ctx.db.list_snapshots(
            svm=snapshot_data.svm,
            volume=snapshot_data.volume,
            name=snapshot_data.name,
            limit=1,
        )
        record = existing[0] if existing else None
        allow_failed_resume = _can_resume_create(record, requested_spec)
        acquired = ctx.db.acquire_snapshot_create_lease(
            snapshot_data.svm,
            snapshot_data.volume,
            snapshot_data.name,
            owner,
            expected_spec=requested_spec.model_dump(mode="json"),
            allow_failed=allow_failed_resume,
            require_ready_volume=True,
        )
        if _can_resume_create(acquired, requested_spec, owner=owner):
            return _resume_snapshot_create(ctx, acquired, owner)
        raise AlreadyExistsError("Snapshot", f"{snapshot_data.svm}/{snapshot_data.volume}/{snapshot_data.name}")

    snapshot = _reconcile_snapshot_create(ctx, snapshot, owner)

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
    volume_lv_name(clone_data.svm, clone_data.name)
    snapshot_lv_name(clone_data.svm, source_volume, clone_data.snapshot)

    ctx = get_context()
    snapshot_size_gib = _clone_snapshot_size_gib(ctx, source_volume, clone_data)
    target_size_gib = max(clone_data.size_gib or snapshot_size_gib, snapshot_size_gib)
    requested_spec = VolumeSpec(
        name=clone_data.name,
        svm=clone_data.svm,
        size_gib=target_size_gib,
        thin=True,
        fs_type="xfs",
    )
    volume = Volume(spec=requested_spec)
    owner = new_create_owner()
    assign_create_lease(volume.status, owner)

    try:
        ctx.db.insert_volume(volume)
    except AlreadyExistsError:
        existing = ctx.db.get_volume(clone_data.svm, clone_data.name)
        allow_failed_resume = _can_resume_clone_volume(existing, requested_spec)
        acquired = ctx.db.acquire_volume_create_lease(
            clone_data.svm,
            clone_data.name,
            owner,
            expected_spec=requested_spec.model_dump(mode="json"),
            allow_failed=allow_failed_resume,
        )
        if _can_resume_clone_volume(acquired, requested_spec, owner=owner):
            return _resume_clone_volume_from_snapshot(
                ctx,
                acquired,
                owner,
                source_volume,
                clone_data.snapshot,
                snapshot_size_gib,
            )
        raise AlreadyExistsError("Volume", f"{clone_data.svm}/{clone_data.name}")

    volume = _reconcile_clone_volume_from_snapshot(
        ctx,
        volume,
        owner,
        source_volume,
        clone_data.snapshot,
        snapshot_size_gib,
    )
    if volume.status.phase == Phase.FAILED:
        raise RuntimeError(volume.status.message)
    return _clone_volume_to_dict(volume, ctx)


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
    return resource_meta_from_record(record)


def _parse_status(record: Dict[str, Any]) -> Any:
    from arca_storage.models.snapshot import SnapshotStatus
    return SnapshotStatus.model_validate(record["status"])


def _clone_snapshot_size_gib(ctx: Any, source_volume: str, clone_data: VolumeCloneCreate) -> int:
    snapshots = ctx.db.list_snapshots(svm=clone_data.svm, volume=source_volume, name=clone_data.snapshot)
    if not snapshots:
        raise NotFoundError("Snapshot", f"{clone_data.svm}/{source_volume}/{clone_data.snapshot}")

    snapshot_record = snapshots[0]
    _require_snapshot_ready_record(snapshot_record, clone_data.svm, source_volume, clone_data.snapshot)
    source_record = ctx.db.get_volume(clone_data.svm, source_volume)
    if not source_record:
        raise PreconditionFailedError(
            f"Snapshot '{clone_data.svm}/{source_volume}/{clone_data.snapshot}' source volume is missing",
            {
                "resource": "Snapshot",
                "name": f"{clone_data.svm}/{source_volume}/{clone_data.snapshot}",
                "source_volume": f"{clone_data.svm}/{source_volume}",
            },
        )
    require_volume_ready_record(source_record, clone_data.svm, source_volume)
    size_gib = snapshot_record.get("status", {}).get("size_gib")
    if size_gib:
        return int(size_gib)

    cfg = ctx.settings.to_reconciler_config()
    vg_name = cfg["vg_name"]
    snap_lv = snapshot_record.get("status", {}).get("lv_name") or snapshot_lv_name(
        clone_data.svm,
        source_volume,
        clone_data.snapshot,
    )
    try:
        return int(ceil(float(ctx.adapters.lvm.get_lv_size_gib(vg_name, snap_lv))))
    except Exception as e:
        raise PreconditionFailedError(
            f"Snapshot '{clone_data.svm}/{source_volume}/{clone_data.snapshot}' size is unavailable",
            {
                "resource": "Snapshot",
                "name": f"{clone_data.svm}/{source_volume}/{clone_data.snapshot}",
                "lv_name": snap_lv,
            },
        ) from e


def _require_snapshot_ready_record(record: Dict[str, Any], svm: str, volume: str, name: str) -> None:
    status = record.get("status", {})
    phase = str(status.get("phase") or "")
    lv_created = bool(status.get("lv_created", False))
    if phase == Phase.READY.value and lv_created:
        return
    raise PreconditionFailedError(
        f"Snapshot '{svm}/{volume}/{name}' is not ready",
        {
            "resource": "Snapshot",
            "name": f"{svm}/{volume}/{name}",
            "phase": phase,
            "lv_created": lv_created,
        },
    )


def _clone_volume_to_dict(vol: Volume, ctx: Any) -> Dict[str, Any]:
    return {
        "name": vol.spec.name,
        "svm": vol.spec.svm,
        "size_gib": vol.spec.size_gib,
        "thin": vol.spec.thin,
        "fs_type": vol.spec.fs_type,
        "status": vol.status.phase.value,
        "lv_path": vol.status.lv_path,
        "lv_name": vol.status.lv_name,
        "mount_path": vol.status.mount_path,
        "export_path": build_volume_export_path(ctx, vol.spec.svm, vol.status.mount_path),
        "created_at": vol.metadata.created_at,
    }


def _can_resume_clone_volume(
    record: Optional[Dict[str, Any]], requested_spec: VolumeSpec, *, owner: Optional[str] = None
) -> bool:
    if not record:
        return False
    status = record.get("status", {})
    phase = status.get("phase")
    if VolumeSpec.model_validate(record["spec"]) != requested_spec:
        return False
    if phase in ACTIVE_CREATE_PHASES:
        return bool(owner and status.get("create_owner") == owner)
    if phase != Phase.FAILED.value:
        return False
    if _is_failed_delete(status):
        return False
    return _has_pending_clone_create_step(status)


def _has_pending_clone_create_step(status: Dict[str, Any]) -> bool:
    return any(not status.get(field, False) for field in ("lv_created", "fs_formatted", "mounted"))


def _resume_clone_volume_from_snapshot(
    ctx: Any,
    record: Dict[str, Any],
    owner: str,
    source_volume: str,
    snapshot_name: str,
    snapshot_size_gib: int,
) -> Dict[str, Any]:
    volume = Volume(
        metadata=_meta_from_record(record),
        spec=VolumeSpec.model_validate(record["spec"]),
        status=_parse_volume_status(record),
    )
    assign_create_lease(volume.status, owner)
    volume.status.message = ""
    volume = _reconcile_clone_volume_from_snapshot(
        ctx,
        volume,
        owner,
        source_volume,
        snapshot_name,
        snapshot_size_gib,
    )
    if volume.status.phase == Phase.FAILED:
        raise RuntimeError(volume.status.message)
    return _clone_volume_to_dict(volume, ctx)


def _parse_volume_status(record: Dict[str, Any]) -> Any:
    from arca_storage.models.volume import VolumeStatus
    return VolumeStatus.model_validate(record["status"])


def _reconcile_clone_volume_from_snapshot(
    ctx: Any,
    volume: Volume,
    owner: str,
    source_volume: str,
    snapshot_name: str,
    snapshot_size_gib: int,
) -> Volume:
    def refresh() -> bool:
        if not ctx.db.refresh_volume_create_lease(volume.spec.svm, volume.spec.name, owner):
            return False
        return extend_create_lease(volume.status, owner)

    with create_lease_heartbeat(refresh):
        return _run_clone_volume_steps(ctx, volume, source_volume, snapshot_name, snapshot_size_gib)


def _run_clone_volume_steps(
    ctx: Any,
    volume: Volume,
    source_volume: str,
    snapshot_name: str,
    snapshot_size_gib: int,
) -> Volume:
    create_owner = volume.status.create_owner
    spec = volume.spec
    cfg = ctx.settings.to_reconciler_config()
    vg_name = cfg["vg_name"]
    export_dir = cfg["export_dir"]
    snap_lv = snapshot_lv_name(spec.svm, source_volume, snapshot_name)
    new_lv = volume_lv_name(spec.svm, spec.name)
    clone_lv_path = f"/dev/{vg_name}/{new_lv}"
    mount_path = volume.status.mount_path or f"{export_dir}/{spec.svm}/{spec.name}"

    if volume.status.lv_created and not ctx.adapters.lvm.lv_exists(vg_name, new_lv):
        volume.status.lv_created = False
        volume.status.lv_path = None
        volume.status.lv_name = None
        volume.status.fs_formatted = False
        volume.status.mounted = False
        volume.status.mount_path = None
        _persist_clone_volume(ctx, volume, "Clone LV state reset", expected_create_owner=create_owner)

    try:
        if not volume.status.lv_created:
            clone_lv_path = create_snapshot_lv_or_accept_existing(
                ctx.adapters.lvm,
                vg_name,
                snap_lv,
                new_lv,
            )
            volume.status.lv_created = True
            volume.status.lv_path = clone_lv_path
            volume.status.lv_name = new_lv
            volume.status.fs_formatted = True
            _persist_clone_volume(ctx, volume, "Clone LV created", expected_create_owner=create_owner)
        else:
            clone_lv_path = volume.status.lv_path or clone_lv_path

        if not volume.status.mounted or not ctx.adapters.xfs.is_mounted(mount_path):
            ctx.adapters.xfs.mount(clone_lv_path, mount_path, extra_options=["nouuid"])
            volume.status.mounted = True
            volume.status.mount_path = mount_path
            volume.status.fs_formatted = True
            _persist_clone_volume(ctx, volume, "Clone LV mounted", expected_create_owner=create_owner)

        if spec.size_gib > snapshot_size_gib:
            ctx.adapters.lvm.resize_lv(vg_name, new_lv, spec.size_gib)
            ctx.adapters.xfs.grow(mount_path)

        volume.status.phase = Phase.READY
        expected_owner = create_owner
        clear_create_lease(volume.status)
        volume.status.message = ""
        _persist_clone_volume(ctx, volume, "Clone volume ready", expected_create_owner=expected_owner)
        return volume
    except CreateLeaseLostError:
        raise
    except Exception as e:
        _cleanup_failed_clone_volume(ctx, volume, vg_name, new_lv, mount_path)
        volume.status.phase = Phase.FAILED
        expected_owner = create_owner
        clear_create_lease(volume.status)
        volume.status.message = f"Clone failed: {e}"
        _persist_clone_volume(ctx, volume, volume.status.message, expected_create_owner=expected_owner)
        return volume


def _cleanup_failed_clone_volume(ctx: Any, volume: Volume, vg_name: str, lv_name: str, mount_path: str) -> None:
    try:
        mounted = volume.status.mounted or ctx.adapters.xfs.is_mounted(mount_path)
    except Exception:
        mounted = volume.status.mounted
    if mounted:
        try:
            ctx.adapters.xfs.umount(mount_path)
        except Exception:
            pass
    if volume.status.lv_created:
        try:
            ctx.adapters.lvm.delete_lv(vg_name, lv_name)
        except Exception:
            pass
    volume.status.lv_created = False
    volume.status.lv_path = None
    volume.status.lv_name = None
    volume.status.fs_formatted = False
    volume.status.mounted = False
    volume.status.mount_path = None


def _persist_clone_volume(
    ctx: Any,
    volume: Volume,
    detail: str,
    *,
    expected_create_owner: Optional[str] = None,
) -> None:
    if not ctx.db.upsert_volume(volume, expected_create_owner=expected_create_owner):
        raise CreateLeaseLostError("Volume", f"{volume.spec.svm}/{volume.spec.name}")
    ctx.db.log_operation("Volume", volume.metadata.id, "clone", volume.status.phase.value, detail)


def _can_resume_create(record: Dict[str, Any], requested_spec: SnapshotSpec, *, owner: Optional[str] = None) -> bool:
    if not record:
        return False
    status = record.get("status", {})
    phase = status.get("phase")
    if SnapshotSpec.model_validate(record["spec"]) != requested_spec:
        return False
    if phase in ACTIVE_CREATE_PHASES:
        return bool(owner and status.get("create_owner") == owner)
    if phase != Phase.FAILED.value:
        return False
    if _is_failed_delete(status):
        return False
    return not status.get("lv_created", False)


def _is_failed_delete(status: Dict[str, Any]) -> bool:
    return str(status.get("message") or "").startswith("Delete failed:")


def _resume_snapshot_create(ctx: Any, record: Dict[str, Any], owner: str) -> Dict[str, Any]:
    snapshot = Snapshot(
        metadata=_meta_from_record(record),
        spec=SnapshotSpec.model_validate(record["spec"]),
        status=_parse_status(record),
    )
    assign_create_lease(snapshot.status, owner)
    snapshot.status.message = ""
    snapshot = _reconcile_snapshot_create(ctx, snapshot, owner)
    if snapshot.status.phase == Phase.FAILED:
        raise RuntimeError(snapshot.status.message)
    return _snapshot_to_dict(snapshot)


def _reconcile_snapshot_create(ctx: Any, snapshot: Snapshot, owner: str) -> Snapshot:
    def refresh() -> bool:
        if not ctx.db.refresh_snapshot_create_lease(
            snapshot.spec.svm,
            snapshot.spec.volume,
            snapshot.spec.name,
            owner,
            require_ready_volume=True,
        ):
            return False
        return extend_create_lease(snapshot.status, owner)

    with create_lease_heartbeat(refresh):
        return ctx.snapshot_reconciler.reconcile(snapshot)
