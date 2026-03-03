"""
Volume service layer.

Delegates to the Volume reconciler for idempotent, step-tracked operations.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from arca_storage.api.models import VolumeCreate
from arca_storage.context import get_context
from arca_storage.errors import NotFoundError
from arca_storage.models.base import Phase
from arca_storage.models.volume import Volume, VolumeSpec
from arca_storage.cli.lib.validators import validate_name


def create_volume(volume_data: VolumeCreate) -> Dict[str, Any]:
    """Create a new volume via the reconciler."""
    validate_name(volume_data.name)
    validate_name(volume_data.svm)

    ctx = get_context()

    volume = Volume(
        spec=VolumeSpec(
            name=volume_data.name,
            svm=volume_data.svm,
            size_gib=volume_data.size_gib,
            thin=volume_data.thin,
            fs_type=volume_data.fs_type,
        ),
    )

    volume = ctx.volume_reconciler.reconcile(volume)

    if volume.status.phase == Phase.FAILED:
        raise RuntimeError(volume.status.message)

    return _volume_to_dict(volume)


def resize_volume(name: str, svm: str, new_size_gib: int) -> Dict[str, Any]:
    """Resize a volume (LV extend + XFS grow)."""
    validate_name(name)
    validate_name(svm)

    ctx = get_context()
    cfg = ctx.settings.to_reconciler_config()
    vg_name = cfg["vg_name"]
    export_dir = cfg["export_dir"]
    lv_name = f"vol_{svm}_{name}"
    mount_path = f"{export_dir}/{svm}/{name}"

    ctx.adapters.lvm.resize_lv(vg_name, lv_name, new_size_gib)
    ctx.adapters.xfs.grow(mount_path)

    # Update volume record in DB
    record = ctx.db.get_volume(svm, name)
    if record:
        vol = Volume(
            metadata=_meta_from_record(record),
            spec=VolumeSpec.model_validate(record["spec"]),
            status=_parse_status(record),
        )
        vol.spec = VolumeSpec(**{**vol.spec.model_dump(), "size_gib": new_size_gib})
        vol.metadata.bump()
        ctx.db.upsert_volume(vol)
        return _volume_to_dict(vol)

    return {"name": name, "svm": svm, "size_gib": new_size_gib, "status": "Ready"}


def delete_volume(name: str, svm: str, force: bool = False) -> None:
    """Delete a volume via the reconciler."""
    validate_name(name)
    validate_name(svm)

    ctx = get_context()
    record = ctx.db.get_volume(svm, name)
    if not record:
        raise NotFoundError("Volume", f"{svm}/{name}")

    volume = Volume(
        metadata=_meta_from_record(record),
        spec=VolumeSpec.model_validate(record["spec"]),
        status=_parse_status(record),
    )
    volume.status.phase = Phase.DELETING
    ctx.volume_reconciler.reconcile(volume)


def list_volumes(
    svm: Optional[str] = None, name: Optional[str] = None, limit: int = 100, cursor: Optional[str] = None
) -> Dict[str, Any]:
    """List volumes from the database."""
    ctx = get_context()
    items = ctx.db.list_volumes(svm=svm, name=name, limit=limit)
    return {"items": items, "next_cursor": None}


def _volume_to_dict(vol: Volume) -> Dict[str, Any]:
    return {
        "name": vol.spec.name,
        "svm": vol.spec.svm,
        "size_gib": vol.spec.size_gib,
        "thin": vol.spec.thin,
        "fs_type": vol.spec.fs_type,
        "mount_path": vol.status.mount_path,
        "lv_path": vol.status.lv_path,
        "lv_name": vol.status.lv_name,
        "status": vol.status.phase.value,
        "created_at": vol.metadata.created_at,
    }


def _meta_from_record(record: Dict[str, Any]) -> Any:
    from arca_storage.models.base import ResourceMeta
    return ResourceMeta(id=record["id"], generation=record.get("generation", 1))


def _parse_status(record: Dict[str, Any]) -> Any:
    from arca_storage.models.volume import VolumeStatus
    return VolumeStatus.model_validate(record["status"])
