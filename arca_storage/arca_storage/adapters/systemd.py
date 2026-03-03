"""
systemd unit management adapter.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from arca_storage.adapters._subprocess import run_cmd


@runtime_checkable
class SystemdAdapter(Protocol):
    def start(self, unit: str) -> None: ...
    def stop(self, unit: str) -> None: ...
    def is_active(self, unit: str) -> bool: ...


class SubprocessSystemdAdapter:
    """Production adapter — calls systemctl."""

    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

    def start(self, unit: str) -> None:
        run_cmd(["systemctl", "start", unit], timeout=self._timeout)

    def stop(self, unit: str) -> None:
        run_cmd(["systemctl", "stop", unit], timeout=self._timeout)

    def is_active(self, unit: str) -> bool:
        result = run_cmd(["systemctl", "is-active", unit], timeout=self._timeout, check=False)
        return result.returncode == 0


class FakeSystemdAdapter:
    """In-memory fake for testing."""

    def __init__(self) -> None:
        self.units: dict[str, bool] = {}  # unit -> active?

    def start(self, unit: str) -> None:
        self.units[unit] = True

    def stop(self, unit: str) -> None:
        self.units[unit] = False

    def is_active(self, unit: str) -> bool:
        return self.units.get(unit, False)
