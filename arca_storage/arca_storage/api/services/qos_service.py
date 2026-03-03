"""
QoS (Quality of Service) management using cgroups v2 I/O Controller.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from arca_storage.context import get_context
from arca_storage.errors import NotFoundError
from arca_storage.cli.lib.validators import validate_name


def _get_cgroup_base() -> Path:
    return Path("/sys/fs/cgroup/arca")


def _get_cgroup_path(svm: str, volume: str) -> Path:
    return _get_cgroup_base() / f"svm_{svm}" / f"vol_{volume}"


def _ensure_cgroup_hierarchy() -> None:
    base_path = _get_cgroup_base()
    if not base_path.exists():
        base_path.mkdir(parents=True, exist_ok=True)


def _get_device_id(lv_path: str) -> str:
    result = subprocess.run(
        ["stat", "--format=%t:%T", lv_path],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to stat device {lv_path}: {result.stderr}")

    hex_major, hex_minor = result.stdout.strip().split(":")
    major = int(hex_major, 16)
    minor = int(hex_minor, 16)
    return f"{major}:{minor}"


def _write_cgroup_file(cgroup_path: Path, filename: str, content: str) -> None:
    file_path = cgroup_path / filename
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)


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
    volumes = ctx.db.list_volumes(svm=svm, name=volume)
    if not volumes:
        raise NotFoundError("Volume", f"{svm}/{volume}")

    volume_info = volumes[0]
    lv_path = volume_info.get("spec", {}).get("lv_path") or volume_info.get("status", {}).get("lv_path")
    if not lv_path:
        raise RuntimeError(f"Volume {volume} has no lv_path")

    _ensure_cgroup_hierarchy()

    cgroup_path = _get_cgroup_path(svm, volume)
    if not cgroup_path.exists():
        cgroup_path.mkdir(parents=True, exist_ok=True)

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

    if not limits:
        io_max_content = f"{device_id} rbps=max wbps=max riops=max wiops=max"
    else:
        io_max_content = f"{device_id} {' '.join(limits)}"

    _write_cgroup_file(cgroup_path, "io.max", io_max_content)

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
    volumes = ctx.db.list_volumes(svm=svm, name=volume)
    if not volumes:
        raise NotFoundError("Volume", f"{svm}/{volume}")

    volume_info = volumes[0]
    lv_path = volume_info.get("spec", {}).get("lv_path") or volume_info.get("status", {}).get("lv_path")
    if not lv_path:
        raise RuntimeError(f"Volume {volume} has no lv_path")

    cgroup_path = _get_cgroup_path(svm, volume)
    if not cgroup_path.exists():
        return

    device_id = _get_device_id(lv_path)
    io_max_content = f"{device_id} rbps=max wbps=max riops=max wiops=max"
    _write_cgroup_file(cgroup_path, "io.max", io_max_content)


def get_qos_settings(svm: str, volume: str) -> Dict[str, Any]:
    """Get current QoS settings for a volume."""
    validate_name(svm)
    validate_name(volume)

    ctx = get_context()
    volumes = ctx.db.list_volumes(svm=svm, name=volume)
    if not volumes:
        raise NotFoundError("Volume", f"{svm}/{volume}")

    volume_info = volumes[0]
    lv_path = volume_info.get("spec", {}).get("lv_path") or volume_info.get("status", {}).get("lv_path")
    if not lv_path:
        raise RuntimeError(f"Volume {volume} has no lv_path")

    cgroup_path = _get_cgroup_path(svm, volume)
    if not cgroup_path.exists():
        return {"svm": svm, "volume": volume, "qos_enabled": False}

    device_id = _get_device_id(lv_path)
    io_max_file = cgroup_path / "io.max"

    if not io_max_file.exists():
        return {"svm": svm, "volume": volume, "qos_enabled": False}

    with open(io_max_file, "r", encoding="utf-8") as f:
        io_max_content = f.read().strip()

    settings: Dict[str, Any] = {
        "svm": svm,
        "volume": volume,
        "qos_enabled": True,
        "device_id": device_id,
        "cgroup_path": str(cgroup_path),
    }

    for line in io_max_content.split("\n"):
        if not line or not line.startswith(device_id):
            continue
        parts = line.split()
        for part in parts[1:]:
            if "=" in part:
                key, value = part.split("=", 1)
                if value == "max":
                    continue
                if key == "rbps":
                    settings["read_bps"] = int(value)
                elif key == "wbps":
                    settings["write_bps"] = int(value)
                elif key == "riops":
                    settings["read_iops"] = int(value)
                elif key == "wiops":
                    settings["write_iops"] = int(value)

    return settings
