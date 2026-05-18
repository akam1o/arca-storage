"""
SVM service layer.

Now delegates to the SVM reconciler for idempotent, step-tracked operations.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from arca_storage.api.models import SVMCreate
from arca_storage.cli.lib.validators import (
    infer_gateway_from_ip_cidr,
    svm_root_lv_name,
    validate_gateway_for_ip_cidr,
    validate_ipv4,
    validate_name,
    validate_svm_ip_cidr,
    validate_vlan,
)
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
from arca_storage.models.base import Phase, resource_meta_from_record
from arca_storage.models.svm import SVM, SVMSpec


def create_svm(svm_data: SVMCreate) -> Dict[str, Any]:
    """Create a new SVM via the reconciler."""
    validate_name(svm_data.name)
    if svm_data.vlan_id is not None:
        validate_vlan(svm_data.vlan_id)
    validate_svm_ip_cidr(svm_data.ip_cidr)
    if svm_data.gateway is not None:
        validate_ipv4(svm_data.gateway)
        if svm_data.vlan_id is not None:
            validate_gateway_for_ip_cidr(svm_data.ip_cidr, svm_data.gateway)
    elif svm_data.vlan_id is not None:
        try:
            infer_gateway_from_ip_cidr(svm_data.ip_cidr)
        except ValueError as e:
            raise InvalidArgumentError(str(e), {"ip_cidr": svm_data.ip_cidr}) from e
    if svm_data.root_volume_size_gib:
        svm_root_lv_name(svm_data.name)

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
        acquired = ctx.db.acquire_svm_create_lease(
            svm_data.name,
            owner,
            expected_spec=requested_spec.model_dump(mode="json"),
            allow_failed=allow_failed_resume,
        )
        if acquired and _can_resume_create(acquired, requested_spec, owner=owner):
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
    volumes = ctx.db.list_all_volumes(svm=name)
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


def require_svm_ready_record(record: Dict[str, Any], name: str) -> None:
    """Reject dependent operations until the SVM reconciler has completed."""
    phase = str(record.get("status", {}).get("phase") or "")
    if phase == Phase.READY.value:
        return
    raise PreconditionFailedError(
        f"SVM '{name}' is not ready",
        {
            "resource": "SVM",
            "name": name,
            "phase": phase,
        },
    )


def delete_svm(name: str, force: bool = False, delete_volumes: bool = False) -> None:
    """Delete an SVM after dependent resources are gone or safely cascaded."""
    validate_name(name)

    ctx = get_context()
    record = ctx.db.reserve_svm_delete(name, force=force, delete_volumes=delete_volumes)
    if not record:
        raise NotFoundError("SVM", name)

    cascade_volumes = delete_volumes or force
    try:
        volumes = ctx.db.list_all_volumes(svm=name)
        if cascade_volumes:
            from arca_storage.api.services import volume_service

            for volume in volumes:
                spec = volume["spec"]
                volume_service.delete_volume(spec["name"], spec["svm"], force=force)

        _cleanup_or_reject_remaining_dependents(ctx, name, force=force)
    except Exception as e:
        _mark_svm_delete_failed(ctx, record, f"Delete failed: {e}")
        raise

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


def _svm_to_dict(svm: SVM, ctx: Optional[Any] = None) -> Dict[str, Any]:
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


def _svm_record_to_dict(record: Dict[str, Any], ctx: Optional[Any] = None) -> Dict[str, Any]:
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
        vip, _prefix = validate_svm_ip_cidr(ip_cidr)
    except ValueError:
        return ""
    return vip


def _export_root(svm_name: str, ctx: Optional[Any] = None) -> str:
    ctx = ctx or get_context()
    export_dir = _safe_export_dir(ctx)
    if not svm_name:
        return export_dir
    try:
        validate_name(svm_name)
    except ValueError:
        return export_dir
    return f"{export_dir}/{svm_name}"


def _safe_export_dir(ctx: Any) -> str:
    raw = str(ctx.settings.to_reconciler_config().get("export_dir", "/exports") or "").strip()
    if not raw or not raw.startswith("/"):
        return "/exports"
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return "/exports"

    parts = [part for part in raw.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return "/exports"
    return "/" + "/".join(parts)


def _meta_from_record(record: Dict[str, Any]) -> Any:
    return resource_meta_from_record(record)


def _parse_status(record: Dict[str, Any], kind: str) -> Any:
    from arca_storage.models.svm import SVMStatus
    return SVMStatus.model_validate(record["status"])


def _can_resume_create(record: Optional[Dict[str, Any]], requested_spec: SVMSpec, *, owner: Optional[str] = None) -> bool:
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
        if not ctx.db.refresh_svm_create_lease(svm.spec.name, owner):
            return False
        return extend_create_lease(svm.status, owner)

    with create_lease_heartbeat(refresh):
        return ctx.svm_reconciler.reconcile(svm)


def _cleanup_or_reject_remaining_dependents(ctx: Any, svm_name: str, *, force: bool) -> None:
    snapshots = ctx.db.list_all_snapshots(svm=svm_name)
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

    exports = ctx.db.list_all_exports(svm=svm_name)
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
            if spec.get("owner", "api") == "api":
                export_service.remove_export(spec["svm"], spec["volume"], spec["client"])
            else:
                export_service.remove_internal_export(spec["svm"], spec["volume"], spec["client"])


def _mark_svm_delete_failed(ctx: Any, record: Dict[str, Any], message: str) -> None:
    svm = SVM(
        metadata=_meta_from_record(record),
        spec=SVMSpec.model_validate(record["spec"]),
        status=_parse_status(record, "svm"),
    )
    svm.status.phase = Phase.FAILED
    svm.status.message = message
    ctx.db.upsert_svm(svm)


def _volume_ref(volume: Dict[str, Any]) -> str:
    spec = volume.get("spec", {})
    return f"{spec.get('svm')}/{spec.get('name')}"


def _snapshot_ref(snapshot: Dict[str, Any]) -> str:
    spec = snapshot.get("spec", {})
    return f"{spec.get('svm')}/{spec.get('volume')}/{spec.get('name')}"


def _export_ref(export: Dict[str, Any]) -> str:
    spec = export.get("spec", {})
    return f"{spec.get('svm')}/{spec.get('volume')}/{spec.get('client')}"
