"""
NFS-Ganesha configuration adapter.
"""

from __future__ import annotations

from typing import Dict, List, Protocol, runtime_checkable

from arca_storage.adapters._subprocess import run_cmd
from arca_storage.cli.lib.ganesha import render_config as _legacy_render_config


@runtime_checkable
class GaneshaAdapter(Protocol):
    def render_config(self, svm_name: str, exports: List[Dict]) -> str: ...
    def reload(self, svm_name: str) -> None: ...


class SubprocessGaneshaAdapter:
    """Production adapter — renders config and reloads via systemctl."""

    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

    def render_config(self, svm_name: str, exports: List[Dict]) -> str:
        return _legacy_render_config(svm_name, exports)

    def reload(self, svm_name: str) -> None:
        run_cmd(
            ["systemctl", "reload", f"nfs-ganesha@{svm_name}"],
            timeout=self._timeout,
        )


class FakeGaneshaAdapter:
    """In-memory fake for testing."""

    def __init__(self, *, fail_count: int = 0) -> None:
        self.configs: dict[str, str] = {}
        self.exports: dict[str, List[Dict]] = {}
        self.reload_count: int = 0
        self._fail_count = fail_count
        self._call_count = 0

    def render_config(self, svm_name: str, exports: List[Dict]) -> str:
        self._maybe_fail()
        path = f"/etc/ganesha/ganesha.{svm_name}.conf"
        self.configs[svm_name] = path
        self.exports[svm_name] = [dict(e) for e in exports]
        return path

    def reload(self, svm_name: str) -> None:
        self._maybe_fail()
        self.reload_count += 1

    def _maybe_fail(self) -> None:
        self._call_count += 1
        if self._call_count <= self._fail_count:
            raise RuntimeError(f"FakeGaneshaAdapter: injected failure #{self._call_count}")
