"""
CSI compatibility layer for directory and quota operations.

The CSI driver models PVCs as directories under an SVM export. Internally Arca
Storage still provisions one LVM/XFS volume per directory name and exposes it
through the SVM's NFS-Ganesha instance.
"""

from __future__ import annotations

import math
import zlib
from typing import Any, Dict

from arca_storage.api.models import DirectoryCreate, QuotaExpand, QuotaSet, VolumeCreate
from arca_storage.api.services import export_service, volume_service
from arca_storage.cli.lib.validators import normalize_ip_cidr, validate_name
from arca_storage.context import get_context
from arca_storage.errors import NotFoundError, PreconditionFailedError

GIB = 1024**3
CSI_ROOT_EXPORT_VOLUME = "__csi_root__"


def create_directory(directory_data: DirectoryCreate) -> Dict[str, Any]:
    """Create an SVM-relative directory for CSI by provisioning a volume."""
    svm = directory_data.svm_name
    path = directory_data.path
    _validate_directory(svm, path)

    ctx = get_context()
    _require_svm(ctx, svm)
    client_cidrs = _csi_client_cidrs(ctx)

    size_gib = _quota_bytes_to_gib(directory_data.quota_bytes)
    volume = _ensure_volume(svm, path, size_gib)
    _ensure_csi_exports(ctx, svm, path, client_cidrs)
    return _directory_response(svm, path, volume)


def delete_directory(svm_name: str, path: str) -> None:
    """Delete a CSI directory and its backing volume."""
    _validate_directory(svm_name, path)

    ctx = get_context()
    _require_svm(ctx, svm_name)
    record = ctx.db.get_volume(svm_name, path)
    if not record:
        raise NotFoundError("Directory", f"{svm_name}/{path}")

    _remove_csi_exports(ctx, svm_name, path)
    volume_service.delete_volume(path, svm_name, force=True)


def set_quota(quota_data: QuotaSet) -> Dict[str, Any]:
    """Set CSI quota/capacity. This maps to backing volume size in GiB."""
    svm = quota_data.svm_name
    path = quota_data.path
    _validate_directory(svm, path)

    ctx = get_context()
    _require_svm(ctx, svm)
    client_cidrs = _csi_client_cidrs(ctx)
    size_gib = _quota_bytes_to_gib(quota_data.quota_bytes)
    _ensure_volume(svm, path, size_gib)
    _ensure_csi_exports(ctx, svm, path, client_cidrs)
    return get_quota(svm, path)


def expand_quota(quota_data: QuotaExpand) -> Dict[str, Any]:
    """Expand CSI quota/capacity."""
    return set_quota(
        QuotaSet(
            svm_name=quota_data.svm_name,
            path=quota_data.path,
            quota_bytes=quota_data.new_quota_bytes,
        )
    )


def get_quota(svm_name: str, path: str) -> Dict[str, Any]:
    """Return quota/capacity information for a CSI directory."""
    _validate_directory(svm_name, path)

    ctx = get_context()
    _require_svm(ctx, svm_name)
    record = ctx.db.get_volume(svm_name, path)
    if not record:
        raise NotFoundError("Quota", f"{svm_name}/{path}")

    size_gib = int(record.get("spec", {}).get("size_gib") or 0)
    quota_bytes = size_gib * GIB
    return {
        "path": path,
        "quota_bytes": quota_bytes,
        "used_bytes": 0,
        "project_id": zlib.crc32(f"{svm_name}/{path}".encode("utf-8")) & 0x7FFFFFFF,
    }


def _ensure_volume(svm: str, path: str, size_gib: int) -> Dict[str, Any]:
    ctx = get_context()
    record = ctx.db.get_volume(svm, path)
    if not record:
        return volume_service.create_volume(
            VolumeCreate(name=path, svm=svm, size_gib=size_gib, thin=True, fs_type="xfs")
        )

    volume_service.require_volume_ready_record(record, svm, path)
    current_size = int(record.get("spec", {}).get("size_gib") or 0)
    if current_size < size_gib:
        return volume_service.resize_volume(path, svm, size_gib)
    return _volume_record_to_dict(record)


def _ensure_csi_exports(ctx: Any, svm: str, path: str, client_cidrs: list[str]) -> None:
    cfg = ctx.settings.to_reconciler_config()
    export_dir = str(cfg.get("export_dir", "/exports")).rstrip("/")
    root_path = f"{export_dir}/{svm}"
    volume_path = f"{root_path}/{path}"
    root_squash = _csi_root_squash(ctx)

    _remove_stale_csi_exports(ctx, svm, set(client_cidrs))

    for client in client_cidrs:
        export_service.ensure_internal_export(
            svm,
            CSI_ROOT_EXPORT_VOLUME,
            client,
            path=root_path,
            pseudo=root_path,
            access="rw",
            root_squash=root_squash,
            owner="csi",
        )
        export_service.ensure_internal_export(
            svm,
            path,
            client,
            path=volume_path,
            pseudo=volume_path,
            access="rw",
            root_squash=root_squash,
            owner="csi",
        )


def _remove_stale_csi_exports(ctx: Any, svm: str, desired_clients: set[str]) -> None:
    for export in ctx.db.list_exports(svm=svm, limit=1_000_000):
        spec = export.get("spec", {})
        volume = spec.get("volume")
        client = spec.get("client")
        if volume and client and spec.get("owner") == "csi" and client not in desired_clients:
            export_service.remove_internal_export(svm, volume, client)


def _remove_csi_exports(ctx: Any, svm: str, path: str) -> None:
    for export in ctx.db.list_exports(svm=svm, volume=path, limit=1_000_000):
        spec = export.get("spec", {})
        if spec.get("owner") == "csi":
            export_service.remove_internal_export(svm, path, spec["client"])

    has_other_csi_volume = any(
        e.get("spec", {}).get("owner") == "csi"
        and e.get("spec", {}).get("volume") != CSI_ROOT_EXPORT_VOLUME
        for e in ctx.db.list_exports(svm=svm, limit=1_000_000)
    )
    if not has_other_csi_volume:
        for export in ctx.db.list_exports(svm=svm, volume=CSI_ROOT_EXPORT_VOLUME, limit=1_000_000):
            spec = export.get("spec", {})
            if spec.get("owner") == "csi":
                export_service.remove_internal_export(svm, CSI_ROOT_EXPORT_VOLUME, spec["client"])


def _csi_client_cidrs(ctx: Any) -> list[str]:
    csi = getattr(ctx.settings, "csi", None)
    client_cidrs = list(getattr(csi, "client_cidrs", []) or [])
    if not client_cidrs:
        raise PreconditionFailedError(
            "CSI NFS client CIDRs are not configured",
            {"resource": "CSIExport", "config": "csi.client_cidrs"},
        )
    return [normalize_ip_cidr(cidr) for cidr in client_cidrs]


def _csi_root_squash(ctx: Any) -> bool:
    csi = getattr(ctx.settings, "csi", None)
    return bool(getattr(csi, "root_squash", True))


def _quota_bytes_to_gib(quota_bytes: int | None) -> int:
    if quota_bytes is None:
        return 1
    return max(1, int(math.ceil(quota_bytes / GIB)))


def _validate_directory(svm: str, path: str) -> None:
    validate_name(svm)
    validate_name(path)


def _require_svm(ctx: Any, svm: str) -> None:
    if not ctx.db.get_svm(svm):
        raise NotFoundError("SVM", svm)


def _volume_record_to_dict(record: Dict[str, Any]) -> Dict[str, Any]:
    spec = record.get("spec", {})
    status = record.get("status", {})
    return {
        "name": spec.get("name"),
        "svm": spec.get("svm"),
        "size_gib": spec.get("size_gib"),
        "thin": spec.get("thin", True),
        "fs_type": spec.get("fs_type", "xfs"),
        "mount_path": status.get("mount_path"),
        "lv_path": status.get("lv_path"),
        "lv_name": status.get("lv_name"),
        "status": status.get("phase"),
        "created_at": record.get("created_at"),
    }


def _directory_response(
    svm: str,
    path: str,
    volume: Dict[str, Any],
) -> Dict[str, Any]:
    size_gib = int(volume.get("size_gib") or 0)
    return {
        "svm_name": svm,
        "path": path,
        "quota_bytes": size_gib * GIB,
        "volume": volume,
    }
