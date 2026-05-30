"""
Helper to run subprocess commands with a mandatory timeout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from arca_storage.errors import SubprocessError
from arca_storage.errors import TimeoutError as ArcaTimeoutError


DEFAULT_TIMEOUT = 30  # seconds


def run_cmd(
    cmd: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command with timeout and structured error reporting."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ArcaTimeoutError(
            operation=_safe_operation_name(cmd), timeout_seconds=timeout
        )

    if check and result.returncode != 0:
        raise SubprocessError(
            cmd=cmd, returncode=result.returncode, stderr=result.stderr.strip()
        )

    return result


def _safe_operation_name(cmd: list[str]) -> str:
    """Return a non-sensitive operation label for API-facing timeout errors."""
    if not cmd:
        return "subprocess"
    return Path(str(cmd[0])).name or "subprocess"
