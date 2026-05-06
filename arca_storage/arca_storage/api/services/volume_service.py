"""
Volume service layer.

Delegates to the Volume reconciler for idempotent, step-tracked operations.
"""

from __future__ import annotations

from ipaddress import IPv4Interface
from typing import Any, Dict, Optional

from arca_storage.api.models import VolumeCreate
from arca_storage.context import get_context
from arca_storage.create_resume import ACTIVE_CREATE_PHASES, is_stale_create_reservation
from arca_storage.db import encode_cursor
from arca_storage.errors import AlreadyExistsError, InternalError, InvalidArgumentError, NotFoundError, PreconditionFailedError
from arca_storage.models.base import Phase
from arca_storage.models.volume import Volume, VolumeSpec
from arca_storage.cli.lib.validators import validate_name

_LIST_ALL_LIMIT = 1_000_000
_CSI_CLIENT_CIDR = "0.0.0.0/0"
_CSI_ROOT_EXPORT_VOLUME = "__csi_root__"


def create_volume(volume_data: VolumeCreate) -> Dict[str, Any]:
    """Create a new volume via the reconciler."""
    validate_name(volume_data.name)
    validate_name(volume_data.svm)

    ctx = get_context()
    if not ctx.db.get_svm(volume_data.svm):
        raise NotFoundError("SVM", volume_data.svm)
    requested_spec = VolumeSpec(
        name=volume_data.name,
        svm=volume_data.svm,
        size_gib=volume_data.size_gib,
        thin=volume_data.thin,
        fs_type=volume_data.fs_type,
    )
    volume = Volume(spec=requested_spec)
    try:
        ctx.db.insert_volume(volume)
    except AlreadyExistsError:
        existing = ctx.db.get_volume(volume_data.svm, volume_data.name)
        if _can_resume_create(existing, requested_spec):
            return _resume_volume_create(ctx, existing)
        raise AlreadyExistsError("Volume", f"{volume_data.svm}/{volume_data.name}")

    volume = ctx.volume_reconciler.reconcile(volume)

    if volume.status.phase == Phase.FAILED:
        raise RuntimeError(volume.status.message)

    return _volume_to_dict(volume, ctx)


def resize_volume(name: str, svm: str, new_size_gib: int) -> Dict[str, Any]:
    """Resize a volume (LV extend + XFS grow)."""
    validate_name(name)
    validate_name(svm)

    ctx = get_context()
    record = ctx.db.get_volume(svm, name)
    if not record:
        raise NotFoundError("Volume", f"{svm}/{name}")

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
    if new_size_gib == current_size:
        vol = Volume(
            metadata=_meta_from_record(record),
            spec=VolumeSpec.model_validate(record["spec"]),
            status=_parse_status(record),
        )
        return _volume_to_dict(vol, ctx)

    cfg = ctx.settings.to_reconciler_config()
    vg_name = cfg["vg_name"]
    export_dir = cfg["export_dir"]
    lv_name = f"vol_{svm}_{name}"
    mount_path = f"{export_dir}/{svm}/{name}"

    ctx.adapters.lvm.resize_lv(vg_name, lv_name, new_size_gib)
    ctx.adapters.xfs.grow(mount_path)

    vol = Volume(
        metadata=_meta_from_record(record),
        spec=VolumeSpec.model_validate(record["spec"]),
        status=_parse_status(record),
    )
    vol.spec = VolumeSpec(**{**vol.spec.model_dump(), "size_gib": new_size_gib})
    vol.metadata.bump()
    ctx.db.upsert_volume(vol)
    return _volume_to_dict(vol, ctx)


def delete_volume(name: str, svm: str, force: bool = False) -> None:
    """Delete a volume and clean up dependent exports/snapshots first."""
    validate_name(name)
    validate_name(svm)

    ctx = get_context()
    record = ctx.db.get_volume(svm, name)
    if not record:
        raise NotFoundError("Volume", f"{svm}/{name}")

    snapshots = ctx.db.list_snapshots(svm=svm, volume=name, limit=_LIST_ALL_LIMIT)
    if snapshots and not force:
        raise PreconditionFailedError(
            f"Volume '{svm}/{name}' has snapshots; delete snapshots first or retry with force",
            {
                "resource": "Volume",
                "name": f"{svm}/{name}",
                "snapshot_count": len(snapshots),
                "snapshots": [_snapshot_ref(s) for s in snapshots],
            },
        )

    _delete_exports_for_volume(ctx, svm, name)

    if snapshots:
        from arca_storage.api.services import snapshot_service

        for snapshot in snapshots:
            spec = snapshot["spec"]
            snapshot_service.delete_snapshot(spec["name"], spec["svm"], spec["volume"], force=True)

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
        raise InvalidArgumentError(str(e), {"cursor": cursor}) from e
    next_cursor = None
    if len(records) > limit:
        spec = records[limit - 1]["spec"]
        next_cursor = encode_cursor([spec["svm"], spec["name"]])
        records = records[:limit]
    items = [_volume_record_to_dict(record, ctx) for record in records]
    return {"items": items, "next_cursor": next_cursor}


def _volume_to_dict(vol: Volume, ctx: Any | None = None) -> Dict[str, Any]:
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
        "export_path": build_volume_export_path(ctx, vol.spec.svm, vol.status.mount_path),
        "status": vol.status.phase.value,
        "created_at": vol.metadata.created_at,
    }


def _volume_record_to_dict(record: Dict[str, Any], ctx: Any | None = None) -> Dict[str, Any]:
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
        "export_path": build_volume_export_path(ctx, spec.get("svm"), mount_path),
        "status": status.get("phase"),
        "created_at": record.get("created_at"),
    }


def build_volume_export_path(ctx: Any, svm: str | None, mount_path: str | None) -> str | None:
    """Return the NFS export location for a mounted volume."""
    if not svm or not mount_path:
        return None

    record = ctx.db.get_svm(svm)
    if not record:
        return None

    ip_cidr = str(record.get("spec", {}).get("ip_cidr") or "")
    try:
        vip = str(IPv4Interface(ip_cidr).ip)
    except Exception:
        vip = ip_cidr.split("/", 1)[0] if ip_cidr else ""
    if not vip:
        return None
    return f"{vip}:{mount_path}"


def _meta_from_record(record: Dict[str, Any]) -> Any:
    from arca_storage.models.base import ResourceMeta
    return ResourceMeta(id=record["id"], generation=record.get("generation", 1))


def _parse_status(record: Dict[str, Any]) -> Any:
    from arca_storage.models.volume import VolumeStatus
    return VolumeStatus.model_validate(record["status"])


def _can_resume_create(record: Dict[str, Any], requested_spec: VolumeSpec) -> bool:
    if not record:
        return False
    status = record.get("status", {})
    phase = status.get("phase")
    if VolumeSpec.model_validate(record["spec"]) != requested_spec:
        return False
    if phase in ACTIVE_CREATE_PHASES:
        return is_stale_create_reservation(record)
    if phase != Phase.FAILED.value:
        return False
    if _is_failed_delete(status):
        return False
    return _has_pending_create_step(status)


def _is_failed_delete(status: Dict[str, Any]) -> bool:
    return str(status.get("message") or "").startswith("Delete failed:")


def _has_pending_create_step(status: Dict[str, Any]) -> bool:
    return any(not status.get(field, False) for field in ("lv_created", "fs_formatted", "mounted"))


def _resume_volume_create(ctx: Any, record: Dict[str, Any]) -> Dict[str, Any]:
    volume = Volume(
        metadata=_meta_from_record(record),
        spec=VolumeSpec.model_validate(record["spec"]),
        status=_parse_status(record),
    )
    volume.status.phase = Phase.CREATING
    volume.status.message = ""
    volume = ctx.volume_reconciler.reconcile(volume)
    if volume.status.phase == Phase.FAILED:
        raise RuntimeError(volume.status.message)
    return _volume_to_dict(volume, ctx)


def _delete_exports_for_volume(ctx: Any, svm: str, volume: str) -> None:
    """Remove DB-backed and CSI-only Ganesha exports before deleting a volume."""
    from arca_storage.api.services import export_service

    exports = ctx.db.list_exports(svm=svm, volume=volume, limit=_LIST_ALL_LIMIT)
    for export in exports:
        spec = export["spec"]
        export_service.remove_export(spec["svm"], spec["volume"], spec["client"])

    _remove_ganesha_exports_for_volume(ctx, svm, volume)


def _remove_ganesha_exports_for_volume(ctx: Any, svm: str, volume: str) -> None:
    from arca_storage.api.services import export_service

    has_other_csi_volume = any(
        e.get("spec", {}).get("owner") == "csi"
        and e.get("spec", {}).get("client") == _CSI_CLIENT_CIDR
        and e.get("spec", {}).get("volume") not in (volume, _CSI_ROOT_EXPORT_VOLUME)
        for e in ctx.db.list_exports(svm=svm, limit=_LIST_ALL_LIMIT)
    )
    if not has_other_csi_volume and ctx.db.get_export(svm, _CSI_ROOT_EXPORT_VOLUME, _CSI_CLIENT_CIDR):
        export_service.remove_internal_export(svm, _CSI_ROOT_EXPORT_VOLUME, _CSI_CLIENT_CIDR)


def _snapshot_ref(snapshot: Dict[str, Any]) -> str:
    spec = snapshot.get("spec", {})
    return f"{spec.get('svm')}/{spec.get('volume')}/{spec.get('name')}"
