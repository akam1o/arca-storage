"""
QoS (Quality of Service) management using cgroups v2 I/O Controller.
"""

from __future__ import annotations

import os
import logging
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from arca_storage.api.services.volume_service import require_volume_ready_record
from arca_storage.cli.lib.validators import validate_name
from arca_storage.context import get_context
from arca_storage.errors import (
    InvalidArgumentError,
    NotFoundError,
    PreconditionFailedError,
)
from arca_storage.openstack.http_errors import safe_error_detail

_QOS_LIMIT_FIELDS = ("read_iops", "write_iops", "read_bps", "write_bps")
logger = logging.getLogger(__name__)


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
        raise RuntimeError("cgroup io controller is not available")

    enabled = subtree_file.read_text(encoding="utf-8").split()
    if "io" not in enabled:
        subtree_file.write_text("+io", encoding="utf-8")


def _get_device_id(lv_path: str) -> str:
    try:
        device_stat = os.stat(lv_path)
    except OSError as e:
        raise RuntimeError("Failed to stat device") from e

    if not stat.S_ISBLK(device_stat.st_mode):
        raise RuntimeError("Path is not a block device")

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
    return [
        line
        for line in io_max_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _io_max_line_device(line: str) -> str:
    return line.split(maxsplit=1)[0] if line.split() else ""


def _write_io_max_limit(cgroup_path: Path, device_id: str, line: str) -> None:
    if _is_kernel_cgroup_path(cgroup_path):
        _write_cgroup_file(cgroup_path, "io.max", line)
        return

    io_max_file = cgroup_path / "io.max"
    lines = [
        existing
        for existing in _read_io_max_lines(io_max_file)
        if _io_max_line_device(existing) != device_id
    ]
    lines.append(line)
    io_max_file.write_text("\n".join(lines), encoding="utf-8")


def _clear_io_max_limit(cgroup_path: Path, device_id: str) -> None:
    reset_line = f"{device_id} rbps=max wbps=max riops=max wiops=max"
    if _is_kernel_cgroup_path(cgroup_path):
        _write_cgroup_file(cgroup_path, "io.max", reset_line)
        return

    io_max_file = cgroup_path / "io.max"
    lines = [
        existing
        for existing in _read_io_max_lines(io_max_file)
        if _io_max_line_device(existing) != device_id
    ]
    io_max_file.write_text("\n".join(lines), encoding="utf-8")


def _require_qos_volume_record(ctx: Any, svm: str, volume: str) -> dict[str, Any]:
    volumes = ctx.db.list_volumes(svm=svm, name=volume)
    if not volumes:
        raise NotFoundError("Volume", f"{svm}/{volume}")

    volume_info = volumes[0]
    require_volume_ready_record(volume_info, svm, volume)
    return volume_info


def _require_qos_volume_lv_path(ctx: Any, svm: str, volume: str) -> str:
    volume_info = _require_qos_volume_record(ctx, svm, volume)
    return _qos_volume_lv_path(volume_info, svm, volume)


def _qos_volume_lv_path(volume_info: dict[str, Any], svm: str, volume: str) -> str:
    lv_path = volume_info.get("spec", {}).get("lv_path") or volume_info.get(
        "status", {}
    ).get("lv_path")
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


def _qos_limits_from_settings(settings: dict[str, Any]) -> dict[str, int]:
    return _normalize_qos_limits(settings, strict=False)


def _normalize_qos_limits(
    raw_limits: dict[str, Any], *, strict: bool
) -> dict[str, int]:
    limits: dict[str, int] = {}
    for field in _QOS_LIMIT_FIELDS:
        value = raw_limits.get(field)
        if value is None:
            continue
        try:
            limits[field] = _normalize_qos_limit_value(field, value)
        except InvalidArgumentError:
            if strict:
                raise
    return limits


def _normalize_qos_limit_value(field: str, value: Any) -> int:
    if isinstance(value, bool):
        raise _invalid_qos_limit_error(field)
    if isinstance(value, int):
        limit = value
    elif isinstance(value, str):
        raw_value = value.strip()
        signed_digits = raw_value[1:] if raw_value[:1] in {"-", "+"} else raw_value
        if not signed_digits.isdigit():
            raise _invalid_qos_limit_error(field)
        limit = int(raw_value)
    else:
        raise _invalid_qos_limit_error(field)

    if limit <= 0:
        raise _invalid_qos_limit_error(field)
    return limit


def _invalid_qos_limit_error(field: str) -> InvalidArgumentError:
    return InvalidArgumentError(
        "QoS limit values must be positive integers",
        {"field": field, "minimum": 1},
    )


def _qos_io_max_line(device_id: str, limits: dict[str, int]) -> str:
    io_limits = []
    if "read_bps" in limits:
        io_limits.append(f"rbps={limits['read_bps']}")
    if "write_bps" in limits:
        io_limits.append(f"wbps={limits['write_bps']}")
    if "read_iops" in limits:
        io_limits.append(f"riops={limits['read_iops']}")
    if "write_iops" in limits:
        io_limits.append(f"wiops={limits['write_iops']}")
    return f"{device_id} {' '.join(io_limits)}"


def _write_qos_limits(
    ctx: Any,
    svm: str,
    volume: str,
    lv_path: str,
    limits: dict[str, int],
) -> Dict[str, Any]:
    limits = _normalize_qos_limits(limits, strict=True)
    _ensure_cgroup_hierarchy()

    cgroup_path = _get_cgroup_path(svm, volume)
    if not cgroup_path.exists():
        cgroup_path.mkdir(parents=True, exist_ok=True)
    _attach_ganesha_process(ctx, svm, cgroup_path)

    device_id = _get_device_id(lv_path)
    _write_io_max_limit(cgroup_path, device_id, _qos_io_max_line(device_id, limits))

    qos_settings: Dict[str, Any] = {
        "svm": svm,
        "volume": volume,
        "qos_enabled": True,
        "device_id": device_id,
        "cgroup_path": str(cgroup_path),
    }
    qos_settings.update(limits)
    return qos_settings


def _disabled_qos_settings(
    svm: str, volume: str, persisted: Optional[dict[str, Any]] = None
) -> Dict[str, Any]:
    settings: Dict[str, Any] = {"svm": svm, "volume": volume, "qos_enabled": False}
    if isinstance(persisted, dict):
        if persisted.get("device_id"):
            settings["device_id"] = persisted["device_id"]
        if persisted.get("cgroup_path"):
            settings["cgroup_path"] = persisted["cgroup_path"]
        settings.update(_qos_limits_from_settings(persisted))
    return settings


def _trusted_persisted_cgroup_path(
    raw_cgroup_path: Any, svm: str, volume: str
) -> Optional[Path]:
    if not raw_cgroup_path:
        return None
    try:
        cgroup_path = Path(str(raw_cgroup_path))
        expected_path = _get_cgroup_path(svm, volume)
        if cgroup_path.resolve(strict=False) != expected_path.resolve(strict=False):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return cgroup_path


def _trusted_persisted_device_id(raw_device_id: Any) -> Optional[str]:
    if raw_device_id is None:
        return None

    device_id = str(raw_device_id).strip()
    major, separator, minor = device_id.partition(":")
    if separator != ":" or not major.isdigit() or not minor.isdigit():
        return None
    return f"{int(major)}:{int(minor)}"


def _qos_failure_detail(exc: Exception) -> str:
    return f"{type(exc).__name__}: {safe_error_detail(exc)}"


def _record_qos_best_effort_failure(
    ctx: Optional[Any],
    svm: str,
    volume: str,
    action: str,
    detail: str,
) -> None:
    if ctx is None:
        return
    db = getattr(ctx, "db", None)
    if db is None or not hasattr(db, "log_operation"):
        return
    try:
        db.log_operation(
            "Volume",
            f"{svm}/{volume}",
            "qos_best_effort",
            "warning",
            f"{action}: {detail}",
        )
    except Exception as e:
        logger.warning(
            "Failed to record QoS best-effort failure for svm=%s volume=%s: %s",
            svm,
            volume,
            _qos_failure_detail(e),
        )


def _log_qos_best_effort_failure(
    ctx: Optional[Any],
    svm: str,
    volume: str,
    action: str,
    exc: Exception,
) -> None:
    detail = _qos_failure_detail(exc)
    logger.warning(
        "QoS %s best-effort failed for svm=%s volume=%s: %s",
        action,
        svm,
        volume,
        detail,
    )
    _record_qos_best_effort_failure(ctx, svm, volume, action, detail)


def _clear_qos_limit_best_effort(
    svm: str,
    volume: str,
    settings: dict[str, Any],
    ctx: Optional[Any] = None,
) -> None:
    raw_cgroup_path = settings.get("cgroup_path")
    cgroup_path = _trusted_persisted_cgroup_path(raw_cgroup_path, svm, volume)
    device_id = _trusted_persisted_device_id(settings.get("device_id"))
    if not cgroup_path or not device_id:
        return
    try:
        if cgroup_path.exists():
            _clear_io_max_limit(cgroup_path, device_id)
    except Exception as e:
        _log_qos_best_effort_failure(ctx, svm, volume, "clear persisted limit", e)


def _clear_qos_limit_for_volume_best_effort(
    svm: str,
    volume: str,
    lv_path: str,
    ctx: Optional[Any] = None,
) -> None:
    try:
        cgroup_path = _get_cgroup_path(svm, volume)
        if cgroup_path.exists():
            _clear_io_max_limit(cgroup_path, _get_device_id(lv_path))
    except Exception as e:
        _log_qos_best_effort_failure(ctx, svm, volume, "clear volume limit", e)


def _restore_qos_limit_direct_best_effort(
    svm: str,
    volume: str,
    lv_path: str,
    settings: dict[str, Any],
    limits: dict[str, int],
    ctx: Optional[Any] = None,
) -> bool:
    raw_cgroup_path = settings.get("cgroup_path")
    raw_device_id = settings.get("device_id")
    try:
        cgroup_path = _trusted_persisted_cgroup_path(
            raw_cgroup_path, svm, volume
        ) or _get_cgroup_path(svm, volume)
        if not cgroup_path.exists():
            return False
        device_id = _trusted_persisted_device_id(raw_device_id) or _get_device_id(
            lv_path
        )
        _write_io_max_limit(cgroup_path, device_id, _qos_io_max_line(device_id, limits))
        return True
    except Exception as e:
        _log_qos_best_effort_failure(ctx, svm, volume, "restore persisted limit", e)
        return False


def _restore_qos_state_best_effort(
    ctx: Any,
    svm: str,
    volume: str,
    lv_path: str,
    settings: Optional[dict[str, Any]],
) -> None:
    if not isinstance(settings, dict):
        _clear_qos_limit_for_volume_best_effort(svm, volume, lv_path, ctx)
        return

    limits = _qos_limits_from_settings(settings)
    if not limits:
        _clear_qos_limit_for_volume_best_effort(svm, volume, lv_path, ctx)
        return

    if _restore_qos_limit_direct_best_effort(
        svm, volume, lv_path, settings, limits, ctx
    ):
        return

    try:
        _write_qos_limits(ctx, svm, volume, lv_path, limits)
    except Exception as e:
        _log_qos_best_effort_failure(ctx, svm, volume, "rewrite limit", e)


def _persist_volume_qos(
    ctx: Any,
    svm: str,
    volume: str,
    qos: Optional[dict[str, Any]],
) -> None:
    persisted = ctx.db.set_volume_qos(svm, volume, qos)
    if not persisted:
        raise NotFoundError("Volume", f"{svm}/{volume}")


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

    raise RuntimeError("Unable to find running NFS-Ganesha process")


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
    volume_info = _require_qos_volume_record(ctx, svm, volume)
    lv_path = _qos_volume_lv_path(volume_info, svm, volume)
    previous_qos = volume_info.get("status", {}).get("qos")

    if all(value is None for value in (read_iops, write_iops, read_bps, write_bps)):
        raise InvalidArgumentError(
            "At least one QoS limit must be specified; use DELETE to remove QoS limits",
            {"fields": list(_QOS_LIMIT_FIELDS)},
        )

    limits = _normalize_qos_limits(
        {
            "read_iops": read_iops,
            "write_iops": write_iops,
            "read_bps": read_bps,
            "write_bps": write_bps,
        },
        strict=True,
    )

    qos_settings = _write_qos_limits(ctx, svm, volume, lv_path, limits)
    try:
        _persist_volume_qos(ctx, svm, volume, qos_settings)
    except NotFoundError:
        _clear_qos_limit_best_effort(svm, volume, qos_settings, ctx)
        raise
    except Exception:
        _restore_qos_state_best_effort(ctx, svm, volume, lv_path, previous_qos)
        raise
    return qos_settings


def remove_qos_from_volume(svm: str, volume: str) -> None:
    """Remove QoS limits from a volume."""
    validate_name(svm)
    validate_name(volume)

    ctx = get_context()
    volume_info = _require_qos_volume_record(ctx, svm, volume)
    lv_path = _qos_volume_lv_path(volume_info, svm, volume)
    previous_qos = volume_info.get("status", {}).get("qos")
    cgroup_path = _get_cgroup_path(svm, volume)
    if cgroup_path.exists():
        device_id = _get_device_id(lv_path)
        _clear_io_max_limit(cgroup_path, device_id)
    try:
        _persist_volume_qos(ctx, svm, volume, None)
    except NotFoundError:
        raise
    except Exception:
        _restore_qos_state_best_effort(ctx, svm, volume, lv_path, previous_qos)
        raise


def get_qos_settings(svm: str, volume: str) -> Dict[str, Any]:
    """Get current QoS settings for a volume."""
    validate_name(svm)
    validate_name(volume)

    ctx = get_context()
    volume_info = _require_qos_volume_record(ctx, svm, volume)
    lv_path = _qos_volume_lv_path(volume_info, svm, volume)
    persisted = volume_info.get("status", {}).get("qos")
    cgroup_path = _get_cgroup_path(svm, volume)
    if not cgroup_path.exists():
        return _disabled_qos_settings(svm, volume, persisted)

    device_id = _get_device_id(lv_path)
    io_max_file = cgroup_path / "io.max"

    if not io_max_file.exists():
        return _disabled_qos_settings(svm, volume, persisted)

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
