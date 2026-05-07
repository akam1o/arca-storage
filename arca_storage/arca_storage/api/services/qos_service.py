"""
QoS (Quality of Service) management using cgroups v2 I/O Controller.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from arca_storage.api.services.volume_service import require_volume_ready_record
from arca_storage.cli.lib.validators import validate_name
from arca_storage.context import get_context
from arca_storage.errors import InvalidArgumentError, NotFoundError, PreconditionFailedError

_QOS_LIMIT_FIELDS = ("read_iops", "write_iops", "read_bps", "write_bps")


def _get_cgroup_base() -> Path:
    return Path("/sys/fs/cgroup/arca")


def _get_cgroup_path(svm: str, _volume: str) -> Path:
    return _get_cgroup_base() / f"svm_{svm}"


def _ensure_cgroup_hierarchy() -> None:
    base_path = _get_cgroup_base()
    if not base_path.exists():
        base_path.mkdir(parents=True, exist_ok=True)
    _enable_io_controller(Path("/sys/fs/cgroup"))
    _enable_io_controller(base_path)


def _enable_io_controller(cgroup_path: Path) -> None:
    controllers_file = cgroup_path / "cgroup.controllers"
    subtree_file = cgroup_path / "cgroup.subtree_control"
    if not controllers_file.exists() or not subtree_file.exists():
        return

    controllers = controllers_file.read_text(encoding="utf-8").split()
    if "io" not in controllers:
        raise RuntimeError(f"cgroup io controller is not available under {cgroup_path}")

    enabled = subtree_file.read_text(encoding="utf-8").split()
    if "io" not in enabled:
        subtree_file.write_text("+io", encoding="utf-8")


def _get_device_id(lv_path: str) -> str:
    try:
        device_stat = os.stat(lv_path)
    except OSError as e:
        raise RuntimeError(f"Failed to stat device {lv_path}: {e}") from e

    if not stat.S_ISBLK(device_stat.st_mode):
        raise RuntimeError(f"Path {lv_path} is not a block device")

    major = os.major(device_stat.st_rdev)
    minor = os.minor(device_stat.st_rdev)
    return f"{major}:{minor}"


def _write_cgroup_file(cgroup_path: Path, filename: str, content: str) -> None:
    file_path = cgroup_path / filename
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)


def _is_kernel_cgroup_path(cgroup_path: Path) -> bool:
    try:
        cgroup_path.resolve().relative_to(Path("/sys/fs/cgroup"))
    except ValueError:
        return False
    return True


def _read_io_max_lines(io_max_file: Path) -> list[str]:
    if not io_max_file.exists():
        return []
    return [line for line in io_max_file.read_text(encoding="utf-8").splitlines() if line.strip()]


def _io_max_line_device(line: str) -> str:
    return line.split(maxsplit=1)[0] if line.split() else ""


def _write_io_max_limit(cgroup_path: Path, device_id: str, line: str) -> None:
    if _is_kernel_cgroup_path(cgroup_path):
        _write_cgroup_file(cgroup_path, "io.max", line)
        return

    io_max_file = cgroup_path / "io.max"
    lines = [existing for existing in _read_io_max_lines(io_max_file) if _io_max_line_device(existing) != device_id]
    lines.append(line)
    io_max_file.write_text("\n".join(lines), encoding="utf-8")


def _clear_io_max_limit(cgroup_path: Path, device_id: str) -> None:
    reset_line = f"{device_id} rbps=max wbps=max riops=max wiops=max"
    if _is_kernel_cgroup_path(cgroup_path):
        _write_cgroup_file(cgroup_path, "io.max", reset_line)
        return

    io_max_file = cgroup_path / "io.max"
    lines = [existing for existing in _read_io_max_lines(io_max_file) if _io_max_line_device(existing) != device_id]
    io_max_file.write_text("\n".join(lines), encoding="utf-8")


def _require_qos_volume_lv_path(ctx: Any, svm: str, volume: str) -> str:
    volumes = ctx.db.list_volumes(svm=svm, name=volume)
    if not volumes:
        raise NotFoundError("Volume", f"{svm}/{volume}")

    volume_info = volumes[0]
    require_volume_ready_record(volume_info, svm, volume)
    lv_path = volume_info.get("spec", {}).get("lv_path") or volume_info.get("status", {}).get("lv_path")
    if not lv_path:
        raise PreconditionFailedError(
            f"Volume '{svm}/{volume}' has no device path",
            {
                "resource": "Volume",
                "name": f"{svm}/{volume}",
                "phase": volume_info.get("status", {}).get("phase"),
            },
        )
    return str(lv_path)


def _attach_ganesha_process(ctx: Any, svm: str, cgroup_path: Path) -> None:
    pid = _get_ganesha_pid(ctx, svm)
    _write_cgroup_file(cgroup_path, "cgroup.procs", str(pid))


def _get_ganesha_pid(ctx: Any, svm: str) -> int:
    svm_record = ctx.db.get_svm(svm) or {}
    uses_host_network = svm_record.get("spec", {}).get("vlan_id") is None
    preferred_unit = "nfs-ganesha-host" if uses_host_network else "nfs-ganesha"
    for unit in dict.fromkeys((preferred_unit, "nfs-ganesha-host", "nfs-ganesha")):
        result = subprocess.run(
            ["systemctl", "show", "--property=MainPID", "--value", f"{unit}@{svm}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode == 0:
            raw = result.stdout.strip()
            if raw.isdigit() and int(raw) > 0:
                return int(raw)

    pid_file = Path(f"/var/run/ganesha.{svm}.pid")
    if pid_file.exists():
        raw = pid_file.read_text(encoding="utf-8").strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)

    raise RuntimeError(f"Unable to find running NFS-Ganesha process for SVM {svm}")


def apply_qos_to_volume(
    svm: str,
    volume: str,
    read_iops: Optional[int] = None,
    write_iops: Optional[int] = None,
    read_bps: Optional[int] = None,
    write_bps: Optional[int] = None,
) -> Dict[str, Any]:
    """Apply QoS limits to a volume using cgroups v2 I/O Controller."""
    validate_name(svm)
    validate_name(volume)

    ctx = get_context()
    lv_path = _require_qos_volume_lv_path(ctx, svm, volume)

    if all(value is None for value in (read_iops, write_iops, read_bps, write_bps)):
        raise InvalidArgumentError(
            "At least one QoS limit must be specified; use DELETE to remove QoS limits",
            {"fields": list(_QOS_LIMIT_FIELDS)},
        )

    _ensure_cgroup_hierarchy()

    cgroup_path = _get_cgroup_path(svm, volume)
    if not cgroup_path.exists():
        cgroup_path.mkdir(parents=True, exist_ok=True)
    _attach_ganesha_process(ctx, svm, cgroup_path)

    device_id = _get_device_id(lv_path)

    limits = []
    if read_bps is not None:
        limits.append(f"rbps={read_bps}")
    if write_bps is not None:
        limits.append(f"wbps={write_bps}")
    if read_iops is not None:
        limits.append(f"riops={read_iops}")
    if write_iops is not None:
        limits.append(f"wiops={write_iops}")

    io_max_content = f"{device_id} {' '.join(limits)}"
    _write_io_max_limit(cgroup_path, device_id, io_max_content)

    qos_settings: Dict[str, Any] = {
        "svm": svm,
        "volume": volume,
        "device_id": device_id,
        "cgroup_path": str(cgroup_path),
    }
    if read_iops is not None:
        qos_settings["read_iops"] = read_iops
    if write_iops is not None:
        qos_settings["write_iops"] = write_iops
    if read_bps is not None:
        qos_settings["read_bps"] = read_bps
    if write_bps is not None:
        qos_settings["write_bps"] = write_bps

    return qos_settings


def remove_qos_from_volume(svm: str, volume: str) -> None:
    """Remove QoS limits from a volume."""
    validate_name(svm)
    validate_name(volume)

    ctx = get_context()
    lv_path = _require_qos_volume_lv_path(ctx, svm, volume)
    cgroup_path = _get_cgroup_path(svm, volume)
    if not cgroup_path.exists():
        return

    device_id = _get_device_id(lv_path)
    _clear_io_max_limit(cgroup_path, device_id)


def get_qos_settings(svm: str, volume: str) -> Dict[str, Any]:
    """Get current QoS settings for a volume."""
    validate_name(svm)
    validate_name(volume)

    ctx = get_context()
    lv_path = _require_qos_volume_lv_path(ctx, svm, volume)
    cgroup_path = _get_cgroup_path(svm, volume)
    if not cgroup_path.exists():
        return {"svm": svm, "volume": volume, "qos_enabled": False}

    device_id = _get_device_id(lv_path)
    io_max_file = cgroup_path / "io.max"

    if not io_max_file.exists():
        return {"svm": svm, "volume": volume, "qos_enabled": False}

    settings: Dict[str, Any] = {
        "svm": svm,
        "volume": volume,
        "qos_enabled": False,
        "device_id": device_id,
        "cgroup_path": str(cgroup_path),
    }

    for line in _read_io_max_lines(io_max_file):
        if _io_max_line_device(line) != device_id:
            continue
        parts = line.split()
        for part in parts[1:]:
            if "=" in part:
                key, value = part.split("=", 1)
                if value == "max":
                    continue
                settings["qos_enabled"] = True
                if key == "rbps":
                    settings["read_bps"] = int(value)
                elif key == "wbps":
                    settings["write_bps"] = int(value)
                elif key == "riops":
                    settings["read_iops"] = int(value)
                elif key == "wiops":
                    settings["write_iops"] = int(value)

    return settings
