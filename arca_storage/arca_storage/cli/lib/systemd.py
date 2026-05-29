"""
systemd unit management functions.
"""

import subprocess


_DEFAULT_COMMAND_TIMEOUT_SECONDS = 30


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    kwargs.setdefault("timeout", _DEFAULT_COMMAND_TIMEOUT_SECONDS)
    return subprocess.run(cmd, **kwargs)


def start_unit(unit_name: str) -> None:
    """
    Start a systemd unit.

    Args:
        unit_name: Unit name (e.g., "nfs-ganesha@svm_name")

    Raises:
        RuntimeError: If starting unit fails
    """
    result = _run(
        ["systemctl", "start", unit_name], capture_output=True, text=True, check=False
    )

    if result.returncode != 0:
        raise RuntimeError("Failed to start systemd unit")


def stop_unit(unit_name: str) -> None:
    """
    Stop a systemd unit.

    Args:
        unit_name: Unit name

    Raises:
        RuntimeError: If stopping unit fails
    """
    result = _run(
        ["systemctl", "stop", unit_name], capture_output=True, text=True, check=False
    )

    if result.returncode != 0:
        raise RuntimeError("Failed to stop systemd unit")


def is_active(unit_name: str) -> bool:
    """
    Check if a systemd unit is active.

    Args:
        unit_name: Unit name

    Returns:
        True if unit is active, False otherwise
    """
    result = _run(
        ["systemctl", "is-active", unit_name],
        capture_output=True,
        text=True,
        check=False,
    )

    return result.returncode == 0
