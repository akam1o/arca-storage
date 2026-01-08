"""
QoS (Quality of Service) management using cgroups v2 I/O Controller.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from arca_storage.cli.lib.config import load_config
from arca_storage.cli.lib.state import list_volumes as state_list_volumes
from arca_storage.cli.lib.validators import validate_name


def _get_cgroup_base() -> Path:
    """
    Get the base cgroup path for ARCA Storage.

    Returns:
        Path to /sys/fs/cgroup/arca
    """
    return Path("/sys/fs/cgroup/arca")


def _get_cgroup_path(svm: str, volume: str) -> Path:
    """
    Get the cgroup path for a specific volume.

    Args:
        svm: SVM name
        volume: Volume name

    Returns:
        Path to /sys/fs/cgroup/arca/svm_{svm}/vol_{volume}
    """
    return _get_cgroup_base() / f"svm_{svm}" / f"vol_{volume}"


def _ensure_cgroup_hierarchy() -> None:
    """
    Ensure the base cgroup hierarchy exists.

    Creates /sys/fs/cgroup/arca if it doesn't exist.

    Raises:
        RuntimeError: If cgroup creation fails
    """
    base_path = _get_cgroup_base()

    if not base_path.exists():
        try:
            base_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"Failed to create cgroup base directory: {e}")


def _get_device_id(lv_path: str) -> str:
    """
    Get the device major:minor number for a logical volume.

    Args:
        lv_path: Path to logical volume (e.g., /dev/vg_arca/vol_svm_volume)

    Returns:
        Device ID in format "major:minor" (e.g., "253:0")

    Raises:
        RuntimeError: If device ID cannot be determined
    """
    try:
        # Use stat to get device numbers
        result = subprocess.run(
            ["stat", "--format=%t:%T", lv_path],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Failed to stat device {lv_path}: {result.stderr}")

        # Convert hex to decimal
        hex_major, hex_minor = result.stdout.strip().split(":")
        major = int(hex_major, 16)
        minor = int(hex_minor, 16)

        return f"{major}:{minor}"

    except Exception as e:
        raise RuntimeError(f"Failed to get device ID for {lv_path}: {e}")


def _write_cgroup_file(cgroup_path: Path, filename: str, content: str) -> None:
    """
    Write content to a cgroup file.

    Args:
        cgroup_path: Path to cgroup directory
        filename: Name of the file to write
        content: Content to write

    Raises:
        RuntimeError: If write fails
    """
    file_path = cgroup_path / filename

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        raise RuntimeError(f"Failed to write to {file_path}: {e}")


def apply_qos_to_volume(
    svm: str,
    volume: str,
    read_iops: Optional[int] = None,
    write_iops: Optional[int] = None,
    read_bps: Optional[int] = None,
    write_bps: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Apply QoS limits to a volume using cgroups v2 I/O Controller.

    Args:
        svm: SVM name
        volume: Volume name
        read_iops: Read IOPS limit (optional)
        write_iops: Write IOPS limit (optional)
        read_bps: Read bandwidth limit in bytes/sec (optional)
        write_bps: Write bandwidth limit in bytes/sec (optional)

    Returns:
        Dictionary with applied QoS settings

    Raises:
        RuntimeError: If QoS application fails
    """
    validate_name(svm)
    validate_name(volume)

    # Find volume in state
    volumes = state_list_volumes(svm=svm, name=volume)
    if not volumes:
        raise RuntimeError(f"Volume {volume} not found in SVM {svm}")

    volume_info = volumes[0]
    lv_path = volume_info.get("lv_path")

    if not lv_path:
        raise RuntimeError(f"Volume {volume} has no lv_path in state")

    # Ensure base cgroup hierarchy exists
    _ensure_cgroup_hierarchy()

    # Create cgroup for this volume
    cgroup_path = _get_cgroup_path(svm, volume)

    if not cgroup_path.exists():
        try:
            cgroup_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"Failed to create cgroup {cgroup_path}: {e}")

    # Get device ID
    device_id = _get_device_id(lv_path)

    # Apply I/O limits using io.max
    # Format: <major>:<minor> rbps=<bytes> wbps=<bytes> riops=<iops> wiops=<iops>
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
        # No limits specified, remove any existing limits
        io_max_content = f"{device_id} rbps=max wbps=max riops=max wiops=max"
    else:
        io_max_content = f"{device_id} {' '.join(limits)}"

    # Write to io.max
    _write_cgroup_file(cgroup_path, "io.max", io_max_content)

    # Store QoS settings
    qos_settings = {
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
    """
    Remove QoS limits from a volume.

    Args:
        svm: SVM name
        volume: Volume name

    Raises:
        RuntimeError: If QoS removal fails
    """
    validate_name(svm)
    validate_name(volume)

    # Find volume in state
    volumes = state_list_volumes(svm=svm, name=volume)
    if not volumes:
        raise RuntimeError(f"Volume {volume} not found in SVM {svm}")

    volume_info = volumes[0]
    lv_path = volume_info.get("lv_path")

    if not lv_path:
        raise RuntimeError(f"Volume {volume} has no lv_path in state")

    # Get cgroup path
    cgroup_path = _get_cgroup_path(svm, volume)

    if not cgroup_path.exists():
        # No cgroup, nothing to remove
        return

    # Get device ID
    device_id = _get_device_id(lv_path)

    # Reset limits to max (no limits)
    io_max_content = f"{device_id} rbps=max wbps=max riops=max wiops=max"
    _write_cgroup_file(cgroup_path, "io.max", io_max_content)

    # Optionally, remove the cgroup directory
    # (keeping it for now in case we want to track QoS history)


def get_qos_settings(svm: str, volume: str) -> Dict[str, Any]:
    """
    Get current QoS settings for a volume.

    Args:
        svm: SVM name
        volume: Volume name

    Returns:
        Dictionary with current QoS settings

    Raises:
        RuntimeError: If volume not found or QoS info cannot be retrieved
    """
    validate_name(svm)
    validate_name(volume)

    # Find volume in state
    volumes = state_list_volumes(svm=svm, name=volume)
    if not volumes:
        raise RuntimeError(f"Volume {volume} not found in SVM {svm}")

    volume_info = volumes[0]
    lv_path = volume_info.get("lv_path")

    if not lv_path:
        raise RuntimeError(f"Volume {volume} has no lv_path in state")

    # Get cgroup path
    cgroup_path = _get_cgroup_path(svm, volume)

    if not cgroup_path.exists():
        return {
            "svm": svm,
            "volume": volume,
            "qos_enabled": False,
        }

    # Get device ID
    device_id = _get_device_id(lv_path)

    # Read io.max
    io_max_file = cgroup_path / "io.max"

    if not io_max_file.exists():
        return {
            "svm": svm,
            "volume": volume,
            "qos_enabled": False,
        }

    try:
        with open(io_max_file, "r", encoding="utf-8") as f:
            io_max_content = f.read().strip()
    except Exception as e:
        raise RuntimeError(f"Failed to read {io_max_file}: {e}")

    # Parse io.max content
    # Format: <major>:<minor> rbps=<bytes> wbps=<bytes> riops=<iops> wiops=<iops>
    settings = {
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
        for part in parts[1:]:  # Skip device_id
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
