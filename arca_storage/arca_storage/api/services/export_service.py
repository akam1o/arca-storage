"""
Export service layer.

Delegates to the Export reconciler for idempotent, step-tracked operations.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from arca_storage.api.models import ExportCreate
from arca_storage.context import get_context
from arca_storage.errors import AlreadyExistsError, InternalError, NotFoundError
from arca_storage.models.base import Phase
from arca_storage.models.export import Export, ExportSpec, ExportStatus
from arca_storage.cli.lib.validators import validate_ip_cidr, validate_name


def add_export(export_data: ExportCreate) -> Dict[str, Any]:
    """Add an NFS export via the reconciler."""
    validate_name(export_data.svm)
    validate_name(export_data.volume)
    validate_ip_cidr(export_data.client)

    ctx = get_context()
    if not ctx.db.get_svm(export_data.svm):
        raise NotFoundError("SVM", export_data.svm)
    if not ctx.db.get_volume(export_data.svm, export_data.volume):
        raise NotFoundError("Volume", f"{export_data.svm}/{export_data.volume}")
    requested_spec = ExportSpec(
        svm=export_data.svm,
        volume=export_data.volume,
        client=export_data.client,
        access=export_data.access,
        root_squash=export_data.root_squash,
        sec=export_data.sec,
    )
    existing = ctx.db.get_export(export_data.svm, export_data.volume, export_data.client)
    if existing:
        if _can_resume_create(existing, requested_spec):
            return _resume_export_create(ctx, existing)
        raise AlreadyExistsError("Export", f"{export_data.svm}/{export_data.volume}/{export_data.client}")

    export = Export(spec=requested_spec)

    export = ctx.export_reconciler.reconcile(export)

    if export.status.phase == Phase.FAILED:
        raise RuntimeError(export.status.message)

    return _export_to_dict(export)


def remove_export(svm: str, volume: str, client: str) -> None:
    """Remove an NFS export via the reconciler."""
    validate_name(svm)
    validate_name(volume)
    validate_ip_cidr(client)

    _remove_export_by_key(svm, volume, client)


def ensure_internal_export(
    svm: str,
    volume: str,
    client: str,
    *,
    path: str,
    pseudo: str,
    access: str = "rw",
    root_squash: bool = False,
    sec: Optional[list[str]] = None,
    owner: str = "internal",
) -> Dict[str, Any]:
    """Create or update an internal export whose path is not derived from volume."""
    validate_name(svm)
    validate_ip_cidr(client)

    export = Export(
        spec=ExportSpec(
            svm=svm,
            volume=volume,
            client=client,
            access=access,
            root_squash=root_squash,
            sec=sec or ["sys"],
            path=path,
            pseudo=pseudo,
            owner=owner,
        ),
    )
    ctx = get_context()
    export = ctx.export_reconciler.reconcile(export)
    if export.status.phase == Phase.FAILED:
        raise RuntimeError(export.status.message)
    return _export_to_dict(export)


def remove_internal_export(svm: str, volume: str, client: str) -> None:
    """Remove an internal export without applying public volume-name validation."""
    validate_name(svm)
    validate_ip_cidr(client)
    _remove_export_by_key(svm, volume, client)


def _remove_export_by_key(svm: str, volume: str, client: str) -> None:
    ctx = get_context()
    record = ctx.db.get_export(svm, volume, client)
    if not record:
        raise NotFoundError("Export", f"{svm}/{volume}/{client}")

    export = Export(
        metadata=_meta_from_record(record),
        spec=ExportSpec.model_validate(record["spec"]),
        status=ExportStatus.model_validate(record["status"]),
    )
    export.status.phase = Phase.DELETING
    result = ctx.export_reconciler.reconcile(export)
    if result.status.phase == Phase.FAILED:
        raise InternalError(
            result.status.message or f"Failed to delete Export '{svm}/{volume}/{client}'",
            {"resource": "Export", "name": f"{svm}/{volume}/{client}"},
        )


def list_exports(
    svm: Optional[str] = None,
    volume: Optional[str] = None,
    client: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """List exports from the database."""
    ctx = get_context()
    items = [
        _export_record_to_dict(record)
        for record in ctx.db.list_exports(svm=svm, volume=volume, client=client, limit=limit)
    ]
    return {"items": items, "next_cursor": None}


def _export_to_dict(export: Export) -> Dict[str, Any]:
    return {
        "svm": export.spec.svm,
        "volume": export.spec.volume,
        "client": export.spec.client,
        "access": export.spec.access,
        "root_squash": export.spec.root_squash,
        "sec": export.spec.sec,
        "pseudo": export.status.pseudo,
        "export_id": export.status.export_id,
        "status": export.status.phase.value,
        "created_at": export.metadata.created_at,
    }


def _export_record_to_dict(record: Dict[str, Any]) -> Dict[str, Any]:
    spec = record.get("spec", {})
    status = record.get("status", {})
    return {
        "svm": spec.get("svm"),
        "volume": spec.get("volume"),
        "client": spec.get("client"),
        "access": spec.get("access"),
        "root_squash": spec.get("root_squash", True),
        "sec": spec.get("sec") or ["sys"],
        "pseudo": status.get("pseudo"),
        "export_id": status.get("export_id"),
        "status": status.get("phase"),
        "created_at": record.get("created_at"),
    }


def _meta_from_record(record: Dict[str, Any]) -> Any:
    from arca_storage.models.base import ResourceMeta
    return ResourceMeta(id=record["id"], generation=record.get("generation", 1))


def _can_resume_create(record: Dict[str, Any], requested_spec: ExportSpec) -> bool:
    status = record.get("status", {})
    phase = status.get("phase")
    if phase not in (Phase.FAILED.value, Phase.CREATING.value):
        return False
    if ExportSpec.model_validate(record["spec"]) != requested_spec:
        return False
    if phase == Phase.CREATING.value:
        return True
    if _is_failed_delete(status):
        return False
    return _has_pending_create_step(status)


def _is_failed_delete(status: Dict[str, Any]) -> bool:
    return str(status.get("message") or "").startswith("Delete failed:")


def _has_pending_create_step(status: Dict[str, Any]) -> bool:
    return not status.get("ganesha_configured", False) or not status.get("service_reloaded", False)


def _resume_export_create(ctx: Any, record: Dict[str, Any]) -> Dict[str, Any]:
    export = Export(
        metadata=_meta_from_record(record),
        spec=ExportSpec.model_validate(record["spec"]),
        status=ExportStatus.model_validate(record["status"]),
    )
    export.status.phase = Phase.CREATING
    export.status.message = ""
    export = ctx.export_reconciler.reconcile(export)
    if export.status.phase == Phase.FAILED:
        raise RuntimeError(export.status.message)
    return _export_to_dict(export)
