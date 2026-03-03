"""
NFS-Ganesha configuration adapter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Protocol, runtime_checkable

from arca_storage.adapters._subprocess import run_cmd
from arca_storage.cli.lib.ganesha import render_config as _legacy_render_config
from arca_storage.cli.lib.ganesha import _load_exports, _save_exports


@runtime_checkable
class GaneshaAdapter(Protocol):
    def render_config(self, svm_name: str, exports: List[Dict]) -> str: ...
    def reload(self, svm_name: str) -> None: ...
    def load_exports(self, svm_name: str) -> List[Dict]: ...
    def save_exports(self, svm_name: str, exports: List[Dict]) -> None: ...


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

    def load_exports(self, svm_name: str) -> List[Dict]:
        return _load_exports(svm_name)

    def save_exports(self, svm_name: str, exports: List[Dict]) -> None:
        _save_exports(svm_name, exports)


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
        return path

    def reload(self, svm_name: str) -> None:
        self._maybe_fail()
        self.reload_count += 1

    def load_exports(self, svm_name: str) -> List[Dict]:
        return list(self.exports.get(svm_name, []))

    def save_exports(self, svm_name: str, exports: List[Dict]) -> None:
        self.exports[svm_name] = list(exports)

    def _maybe_fail(self) -> None:
        self._call_count += 1
        if self._call_count <= self._fail_count:
            raise RuntimeError(f"FakeGaneshaAdapter: injected failure #{self._call_count}")
