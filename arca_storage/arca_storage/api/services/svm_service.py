"""
SVM service layer.

Now delegates to the SVM reconciler for idempotent, step-tracked operations.
"""

from __future__ import annotations

from ipaddress import IPv4Interface
from typing import Any, Dict, Optional

from arca_storage.api.models import SVMCreate
from arca_storage.cli.lib.validators import validate_ip_cidr, validate_ipv4, validate_name, validate_vlan
from arca_storage.context import get_context
from arca_storage.errors import AlreadyExistsError, NotFoundError, PreconditionFailedError
from arca_storage.models.base import Phase
from arca_storage.models.svm import SVM, SVMSpec

_LIST_ALL_LIMIT = 1_000_000


def create_svm(svm_data: SVMCreate) -> Dict[str, Any]:
    """Create a new SVM via the reconciler."""
    validate_name(svm_data.name)
    validate_vlan(svm_data.vlan_id)
    validate_ip_cidr(svm_data.ip_cidr)
    if svm_data.gateway is not None:
        validate_ipv4(svm_data.gateway)

    ctx = get_context()

    # Check for duplicates
    existing = ctx.db.get_svm(svm_data.name)
    if existing:
        raise AlreadyExistsError("SVM", svm_data.name)

    svm = SVM(
        spec=SVMSpec(
            name=svm_data.name,
            vlan_id=svm_data.vlan_id,
            ip_cidr=svm_data.ip_cidr,
            gateway=svm_data.gateway,
            mtu=svm_data.mtu,
            root_volume_size_gib=svm_data.root_volume_size_gib,
        ),
    )

    svm = ctx.svm_reconciler.reconcile(svm)

    if svm.status.phase == Phase.FAILED:
        raise RuntimeError(svm.status.message)

    return _svm_to_dict(svm)


def list_svms(name: Optional[str] = None, limit: int = 100, cursor: Optional[str] = None) -> Dict[str, Any]:
    """List SVMs from the database."""
    ctx = get_context()
    items = [_svm_record_to_dict(record) for record in ctx.db.list_svms(name=name, limit=limit)]
    return {"items": items, "next_cursor": None}


def get_svm(name: str) -> Dict[str, Any]:
    """Get a single SVM by name."""
    validate_name(name)
    ctx = get_context()
    record = ctx.db.get_svm(name)
    if not record:
        raise NotFoundError("SVM", name)
    return _svm_record_to_dict(record)


def delete_svm(name: str, force: bool = False, delete_volumes: bool = False) -> None:
    """Delete an SVM after dependent resources are gone or safely cascaded."""
    validate_name(name)

    ctx = get_context()
    record = ctx.db.get_svm(name)
    if not record:
        raise NotFoundError("SVM", name)

    volumes = ctx.db.list_volumes(svm=name, limit=_LIST_ALL_LIMIT)
    cascade_volumes = delete_volumes or force
    if volumes and not cascade_volumes:
        raise PreconditionFailedError(
            f"SVM '{name}' has volumes; delete volumes first or retry with delete_volumes",
            {
                "resource": "SVM",
                "name": name,
                "volume_count": len(volumes),
                "volumes": [_volume_ref(v) for v in volumes],
            },
        )

    if cascade_volumes:
        from arca_storage.api.services import volume_service

        for volume in volumes:
            spec = volume["spec"]
            volume_service.delete_volume(spec["name"], spec["svm"], force=force)

    _cleanup_or_reject_remaining_dependents(ctx, name, force=force)

    svm = SVM(
        metadata=_meta_from_record(record),
        spec=SVMSpec.model_validate(record["spec"]),
        status=_parse_status(record, "svm"),
    )
    svm.status.phase = Phase.DELETING
    ctx.svm_reconciler.reconcile(svm)


def _svm_to_dict(svm: SVM) -> Dict[str, Any]:
    vip = _vip_from_ip_cidr(svm.spec.ip_cidr)
    return {
        "name": svm.spec.name,
        "vlan_id": svm.spec.vlan_id,
        "ip_cidr": svm.spec.ip_cidr,
        "vip": vip,
        "gateway": svm.spec.gateway,
        "mtu": svm.spec.mtu,
        "namespace": svm.spec.name,
        "status": svm.status.phase.value,
        "state": svm.status.phase.value,
        "created_at": svm.metadata.created_at,
    }


def _svm_record_to_dict(record: Dict[str, Any]) -> Dict[str, Any]:
    spec = record.get("spec", {})
    status = record.get("status", {})
    ip_cidr = str(spec.get("ip_cidr") or "")
    phase = status.get("phase", "unknown")
    return {
        "name": spec.get("name"),
        "vlan_id": spec.get("vlan_id"),
        "ip_cidr": ip_cidr,
        "vip": _vip_from_ip_cidr(ip_cidr),
        "gateway": spec.get("gateway"),
        "mtu": spec.get("mtu", 1500),
        "namespace": spec.get("name"),
        "status": phase,
        "state": phase,
        "created_at": record.get("created_at"),
    }


def _vip_from_ip_cidr(ip_cidr: str) -> str:
    try:
        return str(IPv4Interface(ip_cidr).ip)
    except Exception:
        return ip_cidr.split("/", 1)[0] if ip_cidr else ""


def _meta_from_record(record: Dict[str, Any]) -> Any:
    from arca_storage.models.base import ResourceMeta
    return ResourceMeta(
        id=record["id"],
        generation=record.get("generation", 1),
    )


def _parse_status(record: Dict[str, Any], kind: str) -> Any:
    from arca_storage.models.svm import SVMStatus
    return SVMStatus.model_validate(record["status"])


def _cleanup_or_reject_remaining_dependents(ctx: Any, svm_name: str, *, force: bool) -> None:
    snapshots = ctx.db.list_snapshots(svm=svm_name, limit=_LIST_ALL_LIMIT)
    if snapshots:
        if not force:
            raise PreconditionFailedError(
                f"SVM '{svm_name}' has snapshots; delete snapshots first or retry with force",
                {
                    "resource": "SVM",
                    "name": svm_name,
                    "snapshot_count": len(snapshots),
                    "snapshots": [_snapshot_ref(s) for s in snapshots],
                },
            )

        from arca_storage.api.services import snapshot_service

        for snapshot in snapshots:
            spec = snapshot["spec"]
            snapshot_service.delete_snapshot(spec["name"], spec["svm"], spec["volume"], force=True)

    exports = ctx.db.list_exports(svm=svm_name, limit=_LIST_ALL_LIMIT)
    if exports:
        if not force:
            raise PreconditionFailedError(
                f"SVM '{svm_name}' has exports; delete exports first or retry with force",
                {
                    "resource": "SVM",
                    "name": svm_name,
                    "export_count": len(exports),
                    "exports": [_export_ref(e) for e in exports],
                },
            )

        from arca_storage.api.services import export_service

        for export in exports:
            spec = export["spec"]
            export_service.remove_export(spec["svm"], spec["volume"], spec["client"])

    ganesha_exports = ctx.adapters.ganesha.load_exports(svm_name)
    if ganesha_exports:
        if not force:
            raise PreconditionFailedError(
                f"SVM '{svm_name}' still has Ganesha exports; remove them first or retry with force",
                {
                    "resource": "SVM",
                    "name": svm_name,
                    "ganesha_export_count": len(ganesha_exports),
                },
            )

        ctx.adapters.ganesha.save_exports(svm_name, [])
        ctx.adapters.ganesha.render_config(svm_name, [])
        ctx.adapters.ganesha.reload(svm_name)


def _volume_ref(volume: Dict[str, Any]) -> str:
    spec = volume.get("spec", {})
    return f"{spec.get('svm')}/{spec.get('name')}"


def _snapshot_ref(snapshot: Dict[str, Any]) -> str:
    spec = snapshot.get("spec", {})
    return f"{spec.get('svm')}/{spec.get('volume')}/{spec.get('name')}"


def _export_ref(export: Dict[str, Any]) -> str:
    spec = export.get("spec", {})
    return f"{spec.get('svm')}/{spec.get('volume')}/{spec.get('client')}"
