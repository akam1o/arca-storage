"""
Export service layer.

Delegates to the Export reconciler for idempotent, step-tracked operations.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from arca_storage.api.models import ExportCreate
from arca_storage.context import get_context
from arca_storage.errors import NotFoundError
from arca_storage.models.base import Phase
from arca_storage.models.export import Export, ExportSpec
from arca_storage.cli.lib.validators import validate_ip_cidr, validate_name


def add_export(export_data: ExportCreate) -> Dict[str, Any]:
    """Add an NFS export via the reconciler."""
    validate_name(export_data.svm)
    validate_name(export_data.volume)
    validate_ip_cidr(export_data.client)

    ctx = get_context()

    export = Export(
        spec=ExportSpec(
            svm=export_data.svm,
            volume=export_data.volume,
            client=export_data.client,
            access=export_data.access,
            root_squash=export_data.root_squash,
            sec=export_data.sec,
        ),
    )

    export = ctx.export_reconciler.reconcile(export)

    if export.status.phase == Phase.FAILED:
        raise RuntimeError(export.status.message)

    return _export_to_dict(export)


def remove_export(svm: str, volume: str, client: str) -> None:
    """Remove an NFS export via the reconciler."""
    validate_name(svm)
    validate_name(volume)
    validate_ip_cidr(client)

    ctx = get_context()
    records = ctx.db.list_exports(svm=svm, volume=volume, client=client)
    if not records:
        raise NotFoundError("Export", f"{svm}/{volume}/{client}")

    record = records[0]
    export = Export(
        metadata=_meta_from_record(record),
        spec=ExportSpec.model_validate(record["spec"]),
        status=_parse_status(record),
    )
    export.status.phase = Phase.DELETING
    ctx.export_reconciler.reconcile(export)


def list_exports(
    svm: Optional[str] = None,
    volume: Optional[str] = None,
    client: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """List exports from the database."""
    ctx = get_context()
    items = ctx.db.list_exports(svm=svm, volume=volume, client=client, limit=limit)
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


def _meta_from_record(record: Dict[str, Any]) -> Any:
    from arca_storage.models.base import ResourceMeta
    return ResourceMeta(id=record["id"], generation=record.get("generation", 1))


def _parse_status(record: Dict[str, Any]) -> Any:
    from arca_storage.models.export import ExportStatus
    return ExportStatus.model_validate(record["status"])
