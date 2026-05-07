"""
LVM Thin Provisioning adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from arca_storage.adapters._subprocess import run_cmd
from arca_storage.errors import AlreadyExistsError, NotFoundError, PreconditionFailedError


def _parse_lvm_float(value: str) -> float:
    return float(value.strip().lstrip("<>"))


@dataclass(frozen=True)
class LVInfo:
    size_gib: float
    attr: str = ""
    segtype: str = ""
    origin: str = ""

    @property
    def is_thin_volume(self) -> bool:
        return self.segtype == "thin" or self.attr.startswith("V")

    @property
    def is_snapshot(self) -> bool:
        return bool(self.origin) or self.attr.startswith("s") or self.segtype == "snapshot"


@runtime_checkable
class LVMAdapter(Protocol):
    def lv_exists(self, vg: str, lv: str) -> bool: ...
    def get_lv_info(self, vg: str, lv: str) -> LVInfo: ...
    def get_lv_size_gib(self, vg: str, lv: str) -> float: ...
    def create_thin_lv(self, vg: str, pool: str, lv: str, size_gib: int) -> str: ...
    def create_regular_lv(self, vg: str, lv: str, size_gib: int) -> str: ...
    def resize_lv(self, vg: str, lv: str, new_size_gib: int) -> None: ...
    def delete_lv(self, vg: str, lv: str) -> None: ...
    def create_snapshot(self, vg: str, source_lv: str, snap_lv: str) -> str: ...
    def get_vg_capacity(self, vg: str) -> dict[str, float]: ...


class SubprocessLVMAdapter:
    """Production adapter — calls real LVM commands with timeouts."""

    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

    def lv_exists(self, vg: str, lv: str) -> bool:
        lv_path = f"/dev/{vg}/{lv}"
        result = run_cmd(["lvdisplay", lv_path], timeout=self._timeout, check=False)
        return result.returncode == 0

    def get_lv_size_gib(self, vg: str, lv: str) -> float:
        return self.get_lv_info(vg, lv).size_gib

    def get_lv_info(self, vg: str, lv: str) -> LVInfo:
        lv_path = f"/dev/{vg}/{lv}"
        result = run_cmd(
            [
                "lvs",
                "--noheadings",
                "--units",
                "g",
                "--nosuffix",
                "--separator",
                ",",
                "-o",
                "LV_SIZE,LV_ATTR,SEGTYPE,ORIGIN",
                lv_path,
            ],
            timeout=self._timeout,
        )
        output = result.stdout.strip()
        if not output:
            raise RuntimeError(f"Unexpected lvs output for {lv_path}: {result.stdout.strip()}")
        fields = [field.strip() for field in output.split(",", 3)]
        if len(fields) < 1:
            raise RuntimeError(f"Unexpected lvs output for {lv_path}: {result.stdout.strip()}")
        return LVInfo(
            size_gib=_parse_lvm_float(fields[0]),
            attr=fields[1] if len(fields) > 1 else "",
            segtype=fields[2] if len(fields) > 2 else "",
            origin=fields[3] if len(fields) > 3 else "",
        )

    def create_thin_lv(self, vg: str, pool: str, lv: str, size_gib: int) -> str:
        lv_path = f"/dev/{vg}/{lv}"
        if self.lv_exists(vg, lv):
            raise AlreadyExistsError("LogicalVolume", lv_path)
        run_cmd(
            ["lvcreate", "-V", f"{size_gib}G", "-T", f"{vg}/{pool}", "-n", lv],
            timeout=self._timeout,
        )
        return lv_path

    def create_regular_lv(self, vg: str, lv: str, size_gib: int) -> str:
        lv_path = f"/dev/{vg}/{lv}"
        if self.lv_exists(vg, lv):
            raise AlreadyExistsError("LogicalVolume", lv_path)
        run_cmd(
            ["lvcreate", "-L", f"{size_gib}G", "-n", lv, vg],
            timeout=self._timeout,
        )
        return lv_path

    def resize_lv(self, vg: str, lv: str, new_size_gib: int) -> None:
        lv_path = f"/dev/{vg}/{lv}"
        if not self.lv_exists(vg, lv):
            raise NotFoundError("LogicalVolume", lv_path)
        current_size_gib = self.get_lv_size_gib(vg, lv)
        requested_size_gib = float(new_size_gib)
        if current_size_gib == requested_size_gib:
            return
        if current_size_gib > requested_size_gib:
            raise PreconditionFailedError(
                f"Logical volume '{lv_path}' is already larger than requested size",
                {
                    "resource": "LogicalVolume",
                    "name": lv_path,
                    "current_size_gib": current_size_gib,
                    "requested_size_gib": new_size_gib,
                },
            )
        run_cmd(
            ["lvextend", "-L", f"{new_size_gib}G", lv_path],
            timeout=self._timeout,
        )

    def delete_lv(self, vg: str, lv: str) -> None:
        if not self.lv_exists(vg, lv):
            return  # idempotent
        lv_path = f"/dev/{vg}/{lv}"
        run_cmd(["lvremove", "-f", lv_path], timeout=self._timeout)

    def create_snapshot(self, vg: str, source_lv: str, snap_lv: str) -> str:
        source_path = f"/dev/{vg}/{source_lv}"
        snap_path = f"/dev/{vg}/{snap_lv}"
        if not self.lv_exists(vg, source_lv):
            raise NotFoundError("LogicalVolume", source_path)
        if self.lv_exists(vg, snap_lv):
            raise AlreadyExistsError("Snapshot", snap_path)
        run_cmd(
            ["lvcreate", "--snapshot", "--name", snap_lv, source_path],
            timeout=self._timeout,
        )
        return snap_path

    def get_vg_capacity(self, vg: str) -> dict[str, float]:
        result = run_cmd(
            [
                "vgs",
                "--noheadings",
                "--units",
                "g",
                "--nosuffix",
                "--separator",
                ",",
                "-o",
                "vg_size,vg_free",
                vg,
            ],
            timeout=self._timeout,
        )
        fields = [field.strip() for field in result.stdout.strip().split(",")]
        if len(fields) != 2:
            raise RuntimeError(f"Unexpected vgs output for {vg}: {result.stdout.strip()}")
        total_gb, free_gb = (_parse_lvm_float(fields[0]), _parse_lvm_float(fields[1]))
        return {"total_gb": total_gb, "free_gb": free_gb}


class FakeLVMAdapter:
    """In-memory fake for testing. No root required."""

    def __init__(self) -> None:
        self.volumes: dict[str, int] = {}  # "vg/lv" -> size_gib
        self.kinds: dict[str, str] = {}  # "vg/lv" -> thin|linear|snapshot
        self.origins: dict[str, str] = {}  # "vg/lv" -> origin lv name

    def lv_exists(self, vg: str, lv: str) -> bool:
        return f"{vg}/{lv}" in self.volumes

    def get_lv_size_gib(self, vg: str, lv: str) -> float:
        return self.get_lv_info(vg, lv).size_gib

    def get_lv_info(self, vg: str, lv: str) -> LVInfo:
        key = f"{vg}/{lv}"
        if key not in self.volumes:
            raise NotFoundError("LogicalVolume", f"/dev/{key}")
        kind = self.kinds.get(key, "thin")
        attr = "Vwi-a-tz--" if kind in {"thin", "snapshot"} else "-wi-a-----"
        return LVInfo(
            size_gib=float(self.volumes[key]),
            attr=attr,
            segtype=kind,
            origin=self.origins.get(key, ""),
        )

    def create_thin_lv(self, vg: str, pool: str, lv: str, size_gib: int) -> str:
        key = f"{vg}/{lv}"
        if key in self.volumes:
            raise AlreadyExistsError("LogicalVolume", f"/dev/{key}")
        self.volumes[key] = size_gib
        self.kinds[key] = "thin"
        return f"/dev/{key}"

    def create_regular_lv(self, vg: str, lv: str, size_gib: int) -> str:
        key = f"{vg}/{lv}"
        if key in self.volumes:
            raise AlreadyExistsError("LogicalVolume", f"/dev/{key}")
        self.volumes[key] = size_gib
        self.kinds[key] = "linear"
        return f"/dev/{key}"

    def resize_lv(self, vg: str, lv: str, new_size_gib: int) -> None:
        key = f"{vg}/{lv}"
        if key not in self.volumes:
            raise NotFoundError("LogicalVolume", f"/dev/{key}")
        current_size_gib = self.volumes[key]
        if current_size_gib == new_size_gib:
            return
        if current_size_gib > new_size_gib:
            raise PreconditionFailedError(
                f"Logical volume '/dev/{key}' is already larger than requested size",
                {
                    "resource": "LogicalVolume",
                    "name": f"/dev/{key}",
                    "current_size_gib": current_size_gib,
                    "requested_size_gib": new_size_gib,
                },
            )
        self.volumes[key] = new_size_gib

    def delete_lv(self, vg: str, lv: str) -> None:
        key = f"{vg}/{lv}"
        self.volumes.pop(key, None)
        self.kinds.pop(key, None)
        self.origins.pop(key, None)

    def create_snapshot(self, vg: str, source_lv: str, snap_lv: str) -> str:
        src_key = f"{vg}/{source_lv}"
        snap_key = f"{vg}/{snap_lv}"
        if src_key not in self.volumes:
            raise NotFoundError("LogicalVolume", f"/dev/{src_key}")
        if snap_key in self.volumes:
            raise AlreadyExistsError("Snapshot", f"/dev/{snap_key}")
        self.volumes[snap_key] = self.volumes[src_key]
        self.kinds[snap_key] = "snapshot"
        self.origins[snap_key] = source_lv
        return f"/dev/{snap_key}"

    def get_vg_capacity(self, vg: str) -> dict[str, float]:
        provisioned = float(sum(size for key, size in self.volumes.items() if key.startswith(f"{vg}/")))
        total = max(1000.0, provisioned)
        return {"total_gb": total, "free_gb": max(total - provisioned, 0.0)}
