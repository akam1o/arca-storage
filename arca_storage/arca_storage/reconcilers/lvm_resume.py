"""Helpers for idempotent LVM create-step resume."""

from __future__ import annotations

from dataclasses import dataclass
import math

from arca_storage.adapters.lvm import LVMAdapter, LVInfo
from arca_storage.errors import AlreadyExistsError, PreconditionFailedError


_SIZE_ABS_TOLERANCE_GIB = 0.01


@dataclass(frozen=True)
class CreateSnapshotLVResult:
    path: str
    created: bool


def create_volume_lv_or_accept_existing(
    lvm: LVMAdapter,
    vg: str,
    thinpool: str,
    lv: str,
    size_gib: int,
    *,
    thin: bool,
) -> str:
    """Create an LV, or accept an already-created matching LV after a crash."""
    try:
        if thin:
            return lvm.create_thin_lv(vg, thinpool, lv, size_gib)
        return lvm.create_regular_lv(vg, lv, size_gib)
    except AlreadyExistsError:
        info = lvm.get_lv_info(vg, lv)
        _ensure_volume_lv_matches(info, f"/dev/{vg}/{lv}", size_gib, thin=thin)
        return f"/dev/{vg}/{lv}"


def create_snapshot_lv_or_accept_existing(
    lvm: LVMAdapter,
    vg: str,
    source_lv: str,
    snap_lv: str,
) -> str:
    return create_snapshot_lv_or_accept_existing_with_result(
        lvm, vg, source_lv, snap_lv
    ).path


def create_snapshot_lv_or_accept_existing_with_result(
    lvm: LVMAdapter,
    vg: str,
    source_lv: str,
    snap_lv: str,
) -> CreateSnapshotLVResult:
    """Create a snapshot LV, or accept an existing snapshot of the same origin."""
    try:
        return CreateSnapshotLVResult(
            path=lvm.create_snapshot(vg, source_lv, snap_lv), created=True
        )
    except AlreadyExistsError:
        info = lvm.get_lv_info(vg, snap_lv)
        if not info.is_snapshot:
            raise PreconditionFailedError(
                "Existing logical volume is not a snapshot",
                {
                    "resource": "Snapshot",
                    "segtype": info.segtype,
                },
            )
        if info.origin and info.origin != source_lv:
            raise PreconditionFailedError(
                "Existing snapshot has a different origin",
                {"resource": "Snapshot"},
            )
        return CreateSnapshotLVResult(path=f"/dev/{vg}/{snap_lv}", created=False)


def _ensure_volume_lv_matches(
    info: LVInfo, lv_path: str, size_gib: int, *, thin: bool
) -> None:
    if not math.isclose(
        info.size_gib, float(size_gib), rel_tol=0.0, abs_tol=_SIZE_ABS_TOLERANCE_GIB
    ):
        raise PreconditionFailedError(
            "Existing logical volume has a different size",
            {
                "resource": "LogicalVolume",
                "expected_size_gib": size_gib,
                "actual_size_gib": info.size_gib,
            },
        )
    if info.origin:
        raise PreconditionFailedError(
            "Existing logical volume is a snapshot",
            {"resource": "LogicalVolume"},
        )
    if thin and not info.is_thin_volume:
        raise PreconditionFailedError(
            "Existing logical volume is not thin-provisioned",
            {"resource": "LogicalVolume", "segtype": info.segtype, "attr": info.attr},
        )
    if not thin and info.is_thin_volume:
        raise PreconditionFailedError(
            "Existing logical volume is thin-provisioned",
            {"resource": "LogicalVolume", "segtype": info.segtype, "attr": info.attr},
        )
