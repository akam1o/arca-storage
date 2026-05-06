"""
SVM service layer.

Now delegates to the SVM reconciler for idempotent, step-tracked operations.
"""

from __future__ import annotations

from ipaddress import IPv4Interface
from typing import Any, Dict, Optional

from arca_storage.api.models import SVMCreate
from arca_storage.cli.lib.validators import infer_gateway_from_ip_cidr, validate_ip_cidr, validate_ipv4, validate_name, validate_vlan
from arca_storage.context import get_context
from arca_storage.create_resume import (
    ACTIVE_CREATE_PHASES,
    assign_create_lease,
    create_lease_heartbeat,
    extend_create_lease,
    new_create_owner,
)
from arca_storage.db import encode_cursor
from arca_storage.errors import AlreadyExistsError, InternalError, InvalidArgumentError, NotFoundError, PreconditionFailedError
from arca_storage.models.base import Phase
from arca_storage.models.svm import SVM, SVMSpec

_LIST_ALL_LIMIT = 1_000_000


def create_svm(svm_data: SVMCreate) -> Dict[str, Any]:
    """Create a new SVM via the reconciler."""
    validate_name(svm_data.name)
    if svm_data.vlan_id is not None:
        validate_vlan(svm_data.vlan_id)
    validate_ip_cidr(svm_data.ip_cidr)
    if svm_data.gateway is not None:
        validate_ipv4(svm_data.gateway)
    elif svm_data.vlan_id is not None:
        try:
            infer_gateway_from_ip_cidr(svm_data.ip_cidr)
        except ValueError as e:
            raise InvalidArgumentError(str(e), {"ip_cidr": svm_data.ip_cidr}) from e

    ctx = get_context()

    requested_spec = SVMSpec(
        name=svm_data.name,
        vlan_id=svm_data.vlan_id,
        ip_cidr=svm_data.ip_cidr,
        gateway=svm_data.gateway,
        mtu=svm_data.mtu,
        root_volume_size_gib=svm_data.root_volume_size_gib,
    )
    svm = SVM(spec=requested_spec)
    owner = new_create_owner()
    assign_create_lease(svm.status, owner)
    try:
        ctx.db.insert_svm(svm)
    except AlreadyExistsError:
        existing = ctx.db.get_svm(svm_data.name)
        allow_failed_resume = _can_resume_create(existing, requested_spec)
        acquired = ctx.db.acquire_svm_create_lease(svm_data.name, owner, allow_failed=allow_failed_resume)
        if _can_resume_create(acquired, requested_spec, owner=owner):
            return _resume_svm_create(ctx, acquired, owner)
        raise AlreadyExistsError("SVM", svm_data.name)

    svm = _reconcile_svm_create(ctx, svm, owner)

    if svm.status.phase == Phase.FAILED:
        raise RuntimeError(svm.status.message)

    return _svm_to_dict(svm, ctx)


def list_svms(name: Optional[str] = None, limit: int = 100, cursor: Optional[str] = None) -> Dict[str, Any]:
    """List SVMs from the database."""
    ctx = get_context()
    try:
        records = ctx.db.list_svms(name=name, limit=limit + 1, cursor=cursor)
    except ValueError as e:
        raise InvalidArgumentError(str(e), {"cursor": cursor}) from e
    next_cursor = None
    if len(records) > limit:
        next_cursor = encode_cursor([records[limit - 1]["name"]])
        records = records[:limit]
    items = [_svm_record_to_dict(record, ctx) for record in records]
    return {"items": items, "next_cursor": next_cursor}


def get_svm(name: str) -> Dict[str, Any]:
    """Get a single SVM by name."""
    validate_name(name)
    ctx = get_context()
    record = ctx.db.get_svm(name)
    if not record:
        raise NotFoundError("SVM", name)
    return _svm_record_to_dict(record, ctx)


def get_svm_capacity(name: str) -> Dict[str, Any]:
    """Return capacity statistics for one SVM."""
    validate_name(name)
    ctx = get_context()
    if not ctx.db.get_svm(name):
        raise NotFoundError("SVM", name)

    cfg = ctx.settings.to_reconciler_config()
    vg_name = cfg["vg_name"]
    vg_capacity = ctx.adapters.lvm.get_vg_capacity(vg_name)
    volumes = ctx.db.list_volumes(svm=name, limit=_LIST_ALL_LIMIT)
    provisioned_gb = float(sum(int(v.get("spec", {}).get("size_gib") or 0) for v in volumes))
    total_gb = float(vg_capacity["total_gb"])
    free_gb = float(vg_capacity["free_gb"])
    used_gb = max(total_gb - free_gb, 0.0)

    return {
        "svm": name,
        "vg": vg_name,
        "total_gb": total_gb,
        "free_gb": free_gb,
        "used_gb": used_gb,
        "provisioned_gb": provisioned_gb,
    }


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
    result = ctx.svm_reconciler.reconcile(svm)
    if result.status.phase == Phase.FAILED:
        raise InternalError(
            result.status.message or f"Failed to delete SVM '{name}'",
            {"resource": "SVM", "name": name},
        )


def _svm_to_dict(svm: SVM, ctx: Any | None = None) -> Dict[str, Any]:
    vip = _vip_from_ip_cidr(svm.spec.ip_cidr)
    return {
        "name": svm.spec.name,
        "vlan_id": svm.spec.vlan_id,
        "ip_cidr": svm.spec.ip_cidr,
        "vip": vip,
        "export_root": _export_root(svm.spec.name, ctx),
        "gateway": svm.spec.gateway,
        "mtu": svm.spec.mtu,
        "namespace": svm.spec.name,
        "status": svm.status.phase.value,
        "state": svm.status.phase.value,
        "created_at": svm.metadata.created_at,
    }


def _svm_record_to_dict(record: Dict[str, Any], ctx: Any | None = None) -> Dict[str, Any]:
    spec = record.get("spec", {})
    status = record.get("status", {})
    ip_cidr = str(spec.get("ip_cidr") or "")
    phase = status.get("phase", "unknown")
    return {
        "name": spec.get("name"),
        "vlan_id": spec.get("vlan_id"),
        "ip_cidr": ip_cidr,
        "vip": _vip_from_ip_cidr(ip_cidr),
        "export_root": _export_root(str(spec.get("name") or ""), ctx),
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


def _export_root(svm_name: str, ctx: Any | None = None) -> str:
    ctx = ctx or get_context()
    export_dir = str(ctx.settings.to_reconciler_config().get("export_dir", "/exports")).rstrip("/")
    if not svm_name:
        return export_dir or "/"
    return f"{export_dir}/{svm_name}" if export_dir else f"/{svm_name}"


def _meta_from_record(record: Dict[str, Any]) -> Any:
    from arca_storage.models.base import ResourceMeta
    return ResourceMeta(
        id=record["id"],
        generation=record.get("generation", 1),
    )


def _parse_status(record: Dict[str, Any], kind: str) -> Any:
    from arca_storage.models.svm import SVMStatus
    return SVMStatus.model_validate(record["status"])


def _can_resume_create(record: Dict[str, Any], requested_spec: SVMSpec, *, owner: str | None = None) -> bool:
    if not record:
        return False
    status = record.get("status", {})
    phase = status.get("phase")
    spec = SVMSpec.model_validate(record["spec"])
    if spec != requested_spec:
        return False
    if phase in ACTIVE_CREATE_PHASES:
        return bool(owner and status.get("create_owner") == owner)
    if phase != Phase.FAILED.value:
        return False
    if _is_failed_delete(status):
        return False
    return _has_pending_create_step(spec, status)


def _is_failed_delete(status: Dict[str, Any]) -> bool:
    return str(status.get("message") or "").startswith("Delete failed:")


def _has_pending_create_step(spec: SVMSpec, status: Dict[str, Any]) -> bool:
    fields = ["ganesha_configured", "pacemaker_group_created"]
    if spec.vlan_id is not None:
        fields.extend(["namespace_created", "vlan_attached"])
    if spec.root_volume_size_gib:
        fields.extend(["lv_created", "fs_formatted"])
    return any(not status.get(field, False) for field in fields)


def _resume_svm_create(ctx: Any, record: Dict[str, Any], owner: str) -> Dict[str, Any]:
    svm = SVM(
        metadata=_meta_from_record(record),
        spec=SVMSpec.model_validate(record["spec"]),
        status=_parse_status(record, "svm"),
    )
    assign_create_lease(svm.status, owner)
    svm.status.message = ""
    svm = _reconcile_svm_create(ctx, svm, owner)
    if svm.status.phase == Phase.FAILED:
        raise RuntimeError(svm.status.message)
    return _svm_to_dict(svm, ctx)


def _reconcile_svm_create(ctx: Any, svm: SVM, owner: str) -> SVM:
    def refresh() -> bool:
        if not extend_create_lease(svm.status, owner):
            return False
        return ctx.db.refresh_svm_create_lease(svm.spec.name, owner)

    with create_lease_heartbeat(refresh):
        return ctx.svm_reconciler.reconcile(svm)


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


def _volume_ref(volume: Dict[str, Any]) -> str:
    spec = volume.get("spec", {})
    return f"{spec.get('svm')}/{spec.get('name')}"


def _snapshot_ref(snapshot: Dict[str, Any]) -> str:
    spec = snapshot.get("spec", {})
    return f"{spec.get('svm')}/{spec.get('volume')}/{spec.get('name')}"


def _export_ref(export: Dict[str, Any]) -> str:
    spec = export.get("spec", {})
    return f"{spec.get('svm')}/{spec.get('volume')}/{spec.get('client')}"
