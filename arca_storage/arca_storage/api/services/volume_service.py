"""
Volume service layer.

Delegates to the Volume reconciler for idempotent, step-tracked operations.
"""

from __future__ import annotations

from math import ceil
from typing import Any, Dict, Optional

from arca_storage.api.models import VolumeCreate
from arca_storage.context import get_context
from arca_storage.create_resume import (
    ACTIVE_CREATE_PHASES,
    assign_create_lease,
    create_lease_heartbeat,
    extend_create_lease,
    new_create_owner,
)
from arca_storage.db import encode_cursor
from arca_storage.errors import (
    AlreadyExistsError,
    ConflictError,
    InternalError,
    InvalidArgumentError,
    NotFoundError,
    PreconditionFailedError,
    ReconcileFailedError,
)
from arca_storage.models.base import Phase, resource_meta_from_record
from arca_storage.models.volume import Volume, VolumeSpec
from arca_storage.cli.lib.validators import validate_name, validate_svm_ip_cidr, volume_lv_name
from arca_storage.api.services.svm_service import require_svm_ready_record

_CSI_ROOT_EXPORT_VOLUME = "__csi_root__"


def create_volume(volume_data: VolumeCreate) -> Dict[str, Any]:
    """Create a new volume via the reconciler."""
    validate_name(volume_data.name)
    validate_name(volume_data.svm)
    volume_lv_name(volume_data.svm, volume_data.name)

    ctx = get_context()
    svm_record = ctx.db.get_svm(volume_data.svm)
    if not svm_record:
        raise NotFoundError("SVM", volume_data.svm)
    require_svm_ready_record(svm_record, volume_data.svm)
    requested_spec = VolumeSpec(
        name=volume_data.name,
        svm=volume_data.svm,
        size_gib=volume_data.size_gib,
        thin=volume_data.thin,
        fs_type=volume_data.fs_type,
    )
    volume = Volume(spec=requested_spec)
    owner = new_create_owner()
    assign_create_lease(volume.status, owner)
    try:
        ctx.db.insert_volume(volume, require_ready_svm=True)
    except AlreadyExistsError:
        existing = ctx.db.get_volume(volume_data.svm, volume_data.name)
        allow_failed_resume = _can_resume_create(existing, requested_spec)
        acquired = ctx.db.acquire_volume_create_lease(
            volume_data.svm,
            volume_data.name,
            owner,
            expected_spec=requested_spec.model_dump(mode="json"),
            allow_failed=allow_failed_resume,
            require_ready_svm=True,
        )
        if acquired and _can_resume_create(acquired, requested_spec, owner=owner):
            return _resume_volume_create(ctx, acquired, owner)
        raise AlreadyExistsError("Volume", f"{volume_data.svm}/{volume_data.name}")

    volume = _reconcile_volume_create(ctx, volume, owner)

    if volume.status.phase == Phase.FAILED:
        raise ReconcileFailedError("Volume", f"{volume_data.svm}/{volume_data.name}", volume.status.message)

    return _volume_to_dict(volume, ctx)


def resize_volume(name: str, svm: str, new_size_gib: int) -> Dict[str, Any]:
    """Resize a volume (LV extend + XFS grow)."""
    validate_name(name)
    validate_name(svm)

    ctx = get_context()
    owner = new_create_owner()
    record = ctx.db.reserve_volume_resize(svm, name, owner, new_size_gib)
    if not record:
        raise NotFoundError("Volume", f"{svm}/{name}")
    completed = False
    try:
        require_volume_ready_record(record, svm, name)
        current_size = int(record.get("spec", {}).get("size_gib") or 0)
        if new_size_gib < current_size:
            raise PreconditionFailedError(
                f"Volume '{svm}/{name}' cannot be shrunk",
                {
                    "resource": "Volume",
                    "name": f"{svm}/{name}",
                    "current_size_gib": current_size,
                    "requested_size_gib": new_size_gib,
                },
            )

        vol = Volume(
            metadata=_meta_from_record(record),
            spec=VolumeSpec.model_validate(record["spec"]),
            status=_parse_status(record),
        )
        if new_size_gib == current_size:
            ctx.db.release_volume_resize(svm, name, owner)
            completed = True
            return _volume_to_dict(vol, ctx)

        cfg = ctx.settings.to_reconciler_config()
        vg_name = cfg["vg_name"]
        export_dir = cfg["export_dir"]
        lv_name = vol.status.lv_name or volume_lv_name(svm, name)
        mount_path = vol.status.mount_path or f"{export_dir}/{svm}/{name}"

        def refresh() -> bool:
            return ctx.db.refresh_volume_resize_lease(svm, name, owner)

        with create_lease_heartbeat(refresh):
            try:
                ctx.adapters.lvm.resize_lv(vg_name, lv_name, new_size_gib)
            except PreconditionFailedError as e:
                recovered_size = _recoverable_backend_resize_size(e, current_size, new_size_gib)
                if recovered_size is None:
                    raise
                ctx.adapters.xfs.grow(mount_path)
                if not ctx.db.recover_volume_size_from_backend(svm, name, owner, recovered_size):
                    raise ConflictError(
                        f"Volume '{svm}/{name}' changed during resize recovery",
                        {
                            "resource": "Volume",
                            "name": f"{svm}/{name}",
                            "recovered_size_gib": recovered_size,
                            "requested_size_gib": new_size_gib,
                        },
                    )
                completed = True
                raise PreconditionFailedError(
                    f"Volume '{svm}/{name}' cannot be shrunk",
                    {
                        "resource": "Volume",
                        "name": f"{svm}/{name}",
                        "current_size_gib": recovered_size,
                        "requested_size_gib": new_size_gib,
                    },
                ) from e
            ctx.adapters.xfs.grow(mount_path)

        vol.spec = VolumeSpec(**{**vol.spec.model_dump(), "size_gib": new_size_gib})
        vol.metadata.bump()
        if not ctx.db.complete_volume_resize(vol, owner):
            raise ConflictError(
                f"Volume '{svm}/{name}' changed during resize",
                {
                    "resource": "Volume",
                    "name": f"{svm}/{name}",
                    "requested_size_gib": new_size_gib,
                },
            )
        completed = True
        return _volume_to_dict(vol, ctx)
    finally:
        if not completed:
            ctx.db.release_volume_resize(svm, name, owner)


def delete_volume(name: str, svm: str, force: bool = False) -> None:
    """Delete a volume and clean up dependent exports/snapshots first."""
    validate_name(name)
    validate_name(svm)

    ctx = get_context()
    record = ctx.db.reserve_volume_delete(svm, name, force=force)
    if not record:
        raise NotFoundError("Volume", f"{svm}/{name}")

    try:
        snapshots = ctx.db.list_all_snapshots(svm=svm, volume=name)
        _delete_exports_for_volume(ctx, svm, name)

        if snapshots:
            from arca_storage.api.services import snapshot_service

            for snapshot in snapshots:
                spec = snapshot["spec"]
                snapshot_service.delete_snapshot(spec["name"], spec["svm"], spec["volume"], force=True)
    except Exception as e:
        _mark_volume_delete_failed(ctx, record, f"Delete failed: {e}")
        raise

    volume = Volume(
        metadata=_meta_from_record(record),
        spec=VolumeSpec.model_validate(record["spec"]),
        status=_parse_status(record),
    )
    volume.status.phase = Phase.DELETING
    result = ctx.volume_reconciler.reconcile(volume)
    if result.status.phase == Phase.FAILED:
        raise InternalError(
            result.status.message or f"Failed to delete Volume '{svm}/{name}'",
            {"resource": "Volume", "name": f"{svm}/{name}"},
        )


def list_volumes(
    svm: Optional[str] = None, name: Optional[str] = None, limit: int = 100, cursor: Optional[str] = None
) -> Dict[str, Any]:
    """List volumes from the database."""
    ctx = get_context()
    try:
        records = ctx.db.list_volumes(svm=svm, name=name, limit=limit + 1, cursor=cursor)
    except ValueError as e:
        raise InvalidArgumentError(str(e), {"field": "cursor"}) from e
    next_cursor = None
    if len(records) > limit:
        spec = records[limit - 1]["spec"]
        next_cursor = encode_cursor([spec["svm"], spec["name"]])
        records = records[:limit]
    svm_records: dict[str, Optional[dict[str, Any]]] = {}
    items = [_volume_record_to_dict(record, ctx, svm_records=svm_records) for record in records]
    return {"items": items, "next_cursor": next_cursor}


def _volume_to_dict(vol: Volume, ctx: Optional[Any] = None) -> Dict[str, Any]:
    ctx = ctx or get_context()
    return {
        "name": vol.spec.name,
        "svm": vol.spec.svm,
        "size_gib": vol.spec.size_gib,
        "thin": vol.spec.thin,
        "fs_type": vol.spec.fs_type,
        "mount_path": vol.status.mount_path,
        "lv_path": vol.status.lv_path,
        "lv_name": vol.status.lv_name,
        "export_path": build_volume_export_path(ctx, vol.spec.svm, vol.status.mount_path, vol.spec.name),
        "status": vol.status.phase.value,
        "created_at": vol.metadata.created_at,
    }


def _volume_record_to_dict(
    record: Dict[str, Any],
    ctx: Optional[Any] = None,
    svm_records: Optional[dict[str, Optional[dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    ctx = ctx or get_context()
    spec = record.get("spec", {})
    status = record.get("status", {})
    mount_path = status.get("mount_path")
    return {
        "name": spec.get("name"),
        "svm": spec.get("svm"),
        "size_gib": spec.get("size_gib"),
        "thin": spec.get("thin", True),
        "fs_type": spec.get("fs_type", "xfs"),
        "mount_path": mount_path,
        "lv_path": status.get("lv_path"),
        "lv_name": status.get("lv_name"),
        "export_path": build_volume_export_path(ctx, spec.get("svm"), mount_path, spec.get("name"), svm_records=svm_records),
        "status": status.get("phase"),
        "created_at": record.get("created_at"),
    }


def _recoverable_backend_resize_size(
    error: PreconditionFailedError,
    db_size_gib: int,
    requested_size_gib: int,
) -> Optional[int]:
    if error.details.get("resource") != "LogicalVolume":
        return None
    raw_size = error.details.get("current_size_gib")
    if raw_size is None:
        return None
    try:
        backend_size_gib = int(ceil(float(raw_size)))
    except (TypeError, ValueError):
        return None
    if backend_size_gib <= db_size_gib or backend_size_gib <= requested_size_gib:
        return None
    return backend_size_gib


def build_volume_export_path(
    ctx: Any,
    svm: Optional[str],
    mount_path: Optional[str],
    volume: Optional[str] = None,
    svm_records: Optional[dict[str, Optional[dict[str, Any]]]] = None,
) -> Optional[str]:
    """Return the NFS export location for a mounted volume."""
    if not svm or not mount_path:
        return None

    svm_name = _safe_resource_name(svm)
    if not svm_name:
        return None

    safe_mount_path = _safe_volume_mount_path(svm_name, volume, mount_path)
    if not safe_mount_path:
        return None

    if svm_records is not None:
        if svm_name not in svm_records:
            svm_records[svm_name] = ctx.db.get_svm(svm_name)
        record = svm_records[svm_name]
    else:
        record = ctx.db.get_svm(svm_name)
    if not record:
        return None

    vip = _vip_from_svm_record(record)
    if not vip:
        return None
    return f"{vip}:{safe_mount_path}"


def _vip_from_svm_record(record: Dict[str, Any]) -> Optional[str]:
    ip_cidr = str(record.get("spec", {}).get("ip_cidr") or "")
    try:
        vip, _prefix = validate_svm_ip_cidr(ip_cidr)
    except ValueError:
        return None
    return vip


def _safe_resource_name(value: Any) -> Optional[str]:
    name = str(value or "")
    try:
        validate_name(name)
    except ValueError:
        return None
    return name


def _safe_volume_mount_path(svm: str, volume: Optional[str], mount_path: Any) -> Optional[str]:
    safe_mount_path = _normalize_absolute_volume_path(mount_path)
    if not safe_mount_path:
        return None

    if volume is None:
        return safe_mount_path

    volume_name = _safe_resource_name(volume)
    if not volume_name:
        return None

    parts = [part for part in safe_mount_path.split("/") if part]
    if len(parts) < 3 or parts[-2:] != [svm, volume_name]:
        return None
    return safe_mount_path


def _normalize_absolute_volume_path(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw or not raw.startswith("/"):
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return None

    parts = [part for part in raw.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    return "/" + "/".join(parts)


def require_volume_ready_record(record: Dict[str, Any], svm: str, name: str) -> None:
    """Reject dependent operations until the volume reconciler has completed."""
    phase = str(record.get("status", {}).get("phase") or "")
    if phase == Phase.READY.value:
        return
    raise PreconditionFailedError(
        f"Volume '{svm}/{name}' is not ready",
        {
            "resource": "Volume",
            "name": f"{svm}/{name}",
            "phase": phase,
        },
    )


def _meta_from_record(record: Dict[str, Any]) -> Any:
    return resource_meta_from_record(record)


def _parse_status(record: Dict[str, Any]) -> Any:
    from arca_storage.models.volume import VolumeStatus
    return VolumeStatus.model_validate(record["status"])


def _can_resume_create(
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
    return _has_pending_create_step(status)


def _is_failed_delete(status: Dict[str, Any]) -> bool:
    return str(status.get("message") or "").startswith("Delete failed:")


def _has_pending_create_step(status: Dict[str, Any]) -> bool:
    return any(not status.get(field, False) for field in ("lv_created", "fs_formatted", "mounted"))


def _resume_volume_create(ctx: Any, record: Dict[str, Any], owner: str) -> Dict[str, Any]:
    volume = Volume(
        metadata=_meta_from_record(record),
        spec=VolumeSpec.model_validate(record["spec"]),
        status=_parse_status(record),
    )
    assign_create_lease(volume.status, owner)
    volume.status.message = ""
    volume = _reconcile_volume_create(ctx, volume, owner)
    if volume.status.phase == Phase.FAILED:
        raise ReconcileFailedError("Volume", f"{volume.spec.svm}/{volume.spec.name}", volume.status.message)
    return _volume_to_dict(volume, ctx)


def _reconcile_volume_create(ctx: Any, volume: Volume, owner: str) -> Volume:
    def refresh() -> bool:
        if not ctx.db.refresh_volume_create_lease(
            volume.spec.svm,
            volume.spec.name,
            owner,
            require_ready_svm=True,
        ):
            return False
        return extend_create_lease(volume.status, owner)

    with create_lease_heartbeat(refresh):
        return ctx.volume_reconciler.reconcile(volume)


def _delete_exports_for_volume(ctx: Any, svm: str, volume: str) -> None:
    """Remove DB-backed and CSI-only Ganesha exports before deleting a volume."""
    from arca_storage.api.services import export_service

    exports = ctx.db.list_all_exports(svm=svm, volume=volume)
    for export in exports:
        spec = export["spec"]
        export_service.remove_export(spec["svm"], spec["volume"], spec["client"])

    _remove_ganesha_exports_for_volume(ctx, svm, volume)


def _remove_ganesha_exports_for_volume(ctx: Any, svm: str, volume: str) -> None:
    from arca_storage.api.services import export_service

    has_other_csi_volume = any(
        e.get("spec", {}).get("owner") == "csi"
        and e.get("spec", {}).get("volume") not in (volume, _CSI_ROOT_EXPORT_VOLUME)
        for e in ctx.db.list_all_exports(svm=svm)
    )
    if not has_other_csi_volume:
        for export in ctx.db.list_all_exports(svm=svm, volume=_CSI_ROOT_EXPORT_VOLUME):
            spec = export.get("spec", {})
            if spec.get("owner") == "csi":
                export_service.remove_internal_export(svm, _CSI_ROOT_EXPORT_VOLUME, spec["client"])


def _mark_volume_delete_failed(ctx: Any, record: Dict[str, Any], message: str) -> None:
    volume = Volume(
        metadata=_meta_from_record(record),
        spec=VolumeSpec.model_validate(record["spec"]),
        status=_parse_status(record),
    )
    volume.status.phase = Phase.FAILED
    volume.status.message = message
    ctx.db.upsert_volume(volume)


def _snapshot_ref(snapshot: Dict[str, Any]) -> str:
    spec = snapshot.get("spec", {})
    return f"{spec.get('svm')}/{spec.get('volume')}/{spec.get('name')}"
