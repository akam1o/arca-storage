"""
Export service layer.

Delegates to the Export reconciler for idempotent, step-tracked operations.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from arca_storage.api.models import ExportCreate
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
    InternalError,
    InvalidArgumentError,
    NotFoundError,
    ReconcileFailedError,
)
from arca_storage.models.base import Phase, resource_meta_from_record
from arca_storage.models.export import Export, ExportSpec, ExportStatus
from arca_storage.cli.lib.validators import (
    normalize_ip_cidr,
    normalize_nfs_client_cidr,
    validate_name,
)
from arca_storage.api.services.svm_service import require_svm_ready_record
from arca_storage.api.services.volume_service import require_volume_ready_record


def add_export(export_data: ExportCreate) -> Dict[str, Any]:
    """Add an NFS export via the reconciler."""
    validate_name(export_data.svm)
    validate_name(export_data.volume)
    client = normalize_nfs_client_cidr(export_data.client)

    ctx = get_context()
    svm_record = ctx.db.get_svm(export_data.svm)
    if not svm_record:
        raise NotFoundError("SVM", export_data.svm)
    require_svm_ready_record(svm_record, export_data.svm)
    volume_record = ctx.db.get_volume(export_data.svm, export_data.volume)
    if not volume_record:
        raise NotFoundError("Volume", f"{export_data.svm}/{export_data.volume}")
    require_volume_ready_record(volume_record, export_data.svm, export_data.volume)
    requested_spec = ExportSpec(
        svm=export_data.svm,
        volume=export_data.volume,
        client=client,
        access=export_data.access,
        root_squash=export_data.root_squash,
        sec=export_data.sec,
    )
    existing = ctx.db.get_export(export_data.svm, export_data.volume, client)
    if existing:
        owner = new_create_owner()
        allow_failed_resume = _can_resume_create(existing, requested_spec)
        acquired = ctx.db.acquire_export_create_lease(
            export_data.svm,
            export_data.volume,
            client,
            owner,
            expected_spec=requested_spec.model_dump(mode="json"),
            allow_failed=allow_failed_resume,
            require_ready_svm=True,
        )
        if acquired and _can_resume_create(acquired, requested_spec, owner=owner):
            return _resume_export_create(ctx, acquired, owner)
        raise AlreadyExistsError(
            "Export", f"{export_data.svm}/{export_data.volume}/{client}"
        )

    export = Export(spec=requested_spec)
    owner = new_create_owner()
    assign_create_lease(export.status, owner)

    export = _reconcile_export_create(ctx, export, owner)

    if export.status.phase == Phase.FAILED:
        raise ReconcileFailedError(
            "Export",
            f"{export_data.svm}/{export_data.volume}/{client}",
            export.status.message,
        )

    return _export_to_dict(export)


def remove_export(svm: str, volume: str, client: str) -> None:
    """Remove an NFS export via the reconciler."""
    validate_name(svm)
    validate_name(volume)
    client = normalize_ip_cidr(client)

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
    client = normalize_ip_cidr(client)

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
    export = ctx.export_reconciler.reconcile(export, allow_update=True)
    if export.status.phase == Phase.FAILED:
        raise ReconcileFailedError(
            "Export", f"{svm}/{volume}/{client}", export.status.message
        )
    return _export_to_dict(export)


def remove_internal_export(svm: str, volume: str, client: str) -> None:
    """Remove an internal export without applying public volume-name validation."""
    validate_name(svm)
    client = normalize_ip_cidr(client)
    _remove_export_by_key(svm, volume, client)


def _remove_export_by_key(svm: str, volume: str, client: str) -> None:
    ctx = get_context()
    record = ctx.db.reserve_export_delete(svm, volume, client)
    if not record:
        raise NotFoundError("Export", f"{svm}/{volume}/{client}")

    export = Export(
        metadata=_meta_from_record(record),
        spec=ExportSpec.model_validate(record["spec"]),
        status=ExportStatus.model_validate(record["status"]),
    )
    result = ctx.export_reconciler.reconcile(export)
    if result.status.phase == Phase.FAILED:
        raise InternalError(
            result.status.message
            or f"Failed to delete Export '{svm}/{volume}/{client}'",
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
    if client:
        client = normalize_ip_cidr(client)
    try:
        records = ctx.db.list_exports(
            svm=svm, volume=volume, client=client, limit=limit + 1, cursor=cursor
        )
    except ValueError as e:
        raise InvalidArgumentError(str(e), {"field": "cursor"}) from e
    next_cursor = None
    if len(records) > limit:
        spec = records[limit - 1]["spec"]
        next_cursor = encode_cursor([spec["svm"], spec["volume"], spec["client"]])
        records = records[:limit]
    items = [_export_record_to_dict(record) for record in records]
    return {"items": items, "next_cursor": next_cursor}


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
    return resource_meta_from_record(record)


def _can_resume_create(
    record: Dict[str, Any], requested_spec: ExportSpec, *, owner: Optional[str] = None
) -> bool:
    if not record:
        return False
    status = record.get("status", {})
    phase = status.get("phase")
    if ExportSpec.model_validate(record["spec"]) != requested_spec:
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
    return not status.get("ganesha_configured", False) or not status.get(
        "service_reloaded", False
    )


def _resume_export_create(
    ctx: Any, record: Dict[str, Any], owner: str
) -> Dict[str, Any]:
    export = Export(
        metadata=_meta_from_record(record),
        spec=ExportSpec.model_validate(record["spec"]),
        status=ExportStatus.model_validate(record["status"]),
    )
    assign_create_lease(export.status, owner)
    export.status.message = ""
    export = _reconcile_export_create(ctx, export, owner)
    if export.status.phase == Phase.FAILED:
        raise ReconcileFailedError(
            "Export",
            f"{export.spec.svm}/{export.spec.volume}/{export.spec.client}",
            export.status.message,
        )
    return _export_to_dict(export)


def _reconcile_export_create(ctx: Any, export: Export, owner: str) -> Export:
    def refresh() -> bool:
        if not ctx.db.refresh_export_create_lease(
            export.spec.svm,
            export.spec.volume,
            export.spec.client,
            owner,
            require_ready_svm=True,
        ):
            return False
        return extend_create_lease(export.status, owner)

    with create_lease_heartbeat(refresh):
        return ctx.export_reconciler.reconcile(export)
