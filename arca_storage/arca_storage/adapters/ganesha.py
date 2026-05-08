"""
NFS-Ganesha configuration adapter.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Protocol, runtime_checkable

from arca_storage.adapters._subprocess import run_cmd
from arca_storage.cli.lib.ganesha import render_config as _legacy_render_config
from arca_storage.config import ArcaSettings


@runtime_checkable
class GaneshaAdapter(Protocol):
    def render_config(
        self,
        svm_name: str,
        exports: List[Dict],
        *,
        bind_addr: Optional[str] = None,
        host_network: bool = False,
    ) -> str: ...
    def reload(self, svm_name: str, *, host_network: bool = False) -> None: ...


class SubprocessGaneshaAdapter:
    """Production adapter — renders config and reloads via systemctl."""

    def __init__(self, timeout: int = 30, settings: Optional[ArcaSettings] = None) -> None:
        self._timeout = timeout
        self._settings = settings

    def render_config(
        self,
        svm_name: str,
        exports: List[Dict],
        *,
        bind_addr: Optional[str] = None,
        host_network: bool = False,
    ) -> str:
        return _legacy_render_config(svm_name, exports, bind_addr=bind_addr, settings=self._settings)

    def reload(self, svm_name: str, *, host_network: bool = False) -> None:
        unit = "nfs-ganesha-host" if host_network else "nfs-ganesha"
        run_cmd(
            ["systemctl", "reload", f"{unit}@{svm_name}"],
            timeout=self._timeout,
        )


class FakeGaneshaAdapter:
    """In-memory fake for testing."""

    def __init__(self, *, fail_count: int = 0) -> None:
        self.configs: dict[str, str] = {}
        self.exports: dict[str, List[Dict]] = {}
        self.bind_addrs: dict[str, Optional[str]] = {}
        self.host_network: dict[str, bool] = {}
        self.reload_count: int = 0
        self._fail_count = fail_count
        self._call_count = 0

    def render_config(
        self,
        svm_name: str,
        exports: List[Dict],
        *,
        bind_addr: Optional[str] = None,
        host_network: bool = False,
    ) -> str:
        self._maybe_fail()
        path = f"/etc/ganesha/ganesha.{svm_name}.conf"
        self.configs[svm_name] = path
        self.exports[svm_name] = [dict(e) for e in exports]
        self.bind_addrs[svm_name] = bind_addr
        self.host_network[svm_name] = host_network
        return path

    def reload(self, svm_name: str, *, host_network: bool = False) -> None:
        self._maybe_fail()
        self.host_network[svm_name] = host_network
        self.reload_count += 1

    def _maybe_fail(self) -> None:
        self._call_count += 1
        if self._call_count <= self._fail_count:
            raise RuntimeError(f"FakeGaneshaAdapter: injected failure #{self._call_count}")
