"""
Network namespace / VLAN adapter.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from arca_storage.adapters._subprocess import run_cmd
from arca_storage.cli.lib.netns import allocate_vlan_ifname  # reuse naming logic


@runtime_checkable
class NetNSAdapter(Protocol):
    def namespace_exists(self, name: str) -> bool: ...
    def create_namespace(self, name: str) -> None: ...
    def delete_namespace(self, name: str) -> None: ...
    def attach_vlan(
        self,
        namespace: str,
        parent_if: str,
        vlan_id: int,
        ip_cidr: str,
        gateway: Optional[str],
        mtu: int,
        ifname: Optional[str],
    ) -> str: ...


class SubprocessNetNSAdapter:
    """Production adapter — calls ip netns / ip link commands."""

    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

    def namespace_exists(self, name: str) -> bool:
        result = run_cmd(["ip", "netns", "list"], timeout=self._timeout, check=False)
        return name in (result.stdout or "")

    def create_namespace(self, name: str) -> None:
        if self.namespace_exists(name):
            return  # idempotent
        run_cmd(["ip", "netns", "add", name], timeout=self._timeout)

    def delete_namespace(self, name: str) -> None:
        if not self.namespace_exists(name):
            return  # idempotent
        run_cmd(["ip", "netns", "del", name], timeout=self._timeout)

    def attach_vlan(
        self,
        namespace: str,
        parent_if: str,
        vlan_id: int,
        ip_cidr: str,
        gateway: Optional[str] = None,
        mtu: int = 1500,
        ifname: Optional[str] = None,
    ) -> str:
        vlan_if = ifname or allocate_vlan_ifname(namespace, vlan_id)

        # Check if interface already exists
        exists_in_root = run_cmd(
            ["ip", "link", "show", vlan_if], timeout=self._timeout, check=False
        ).returncode == 0

        exists_in_ns = run_cmd(
            ["ip", "netns", "exec", namespace, "ip", "link", "show", vlan_if],
            timeout=self._timeout,
            check=False,
        ).returncode == 0

        if exists_in_ns:
            self._configure_ip(namespace, vlan_if, ip_cidr, gateway, mtu)
            return vlan_if

        if exists_in_root:
            run_cmd(
                ["ip", "link", "set", vlan_if, "netns", namespace],
                timeout=self._timeout,
            )
        else:
            run_cmd(
                [
                    "ip", "link", "add", "link", parent_if,
                    "name", vlan_if, "type", "vlan", "id", str(vlan_id),
                ],
                timeout=self._timeout,
            )
            run_cmd(
                ["ip", "link", "set", vlan_if, "netns", namespace],
                timeout=self._timeout,
            )

        self._configure_ip(namespace, vlan_if, ip_cidr, gateway, mtu)
        return vlan_if

    def _configure_ip(
        self,
        namespace: str,
        interface: str,
        ip_cidr: str,
        gateway: Optional[str],
        mtu: int,
    ) -> None:
        if mtu != 1500:
            run_cmd(
                ["ip", "netns", "exec", namespace, "ip", "link", "set", interface, "mtu", str(mtu)],
                timeout=self._timeout,
            )
        result = run_cmd(
            ["ip", "netns", "exec", namespace, "ip", "addr", "show", interface],
            timeout=self._timeout,
            check=False,
        )
        if ip_cidr not in (result.stdout or ""):
            run_cmd(
                ["ip", "netns", "exec", namespace, "ip", "addr", "add", ip_cidr, "dev", interface],
                timeout=self._timeout,
            )
        run_cmd(
            ["ip", "netns", "exec", namespace, "ip", "link", "set", interface, "up"],
            timeout=self._timeout,
        )
        if gateway:
            run_cmd(
                ["ip", "netns", "exec", namespace, "ip", "route", "del", "default"],
                timeout=self._timeout,
                check=False,
            )
            run_cmd(
                ["ip", "netns", "exec", namespace, "ip", "route", "add", "default", "via", gateway],
                timeout=self._timeout,
            )


class FakeNetNSAdapter:
    """In-memory fake for testing."""

    def __init__(self) -> None:
        self.namespaces: dict[str, dict] = {}  # name -> {vlans: [...]}

    def namespace_exists(self, name: str) -> bool:
        return name in self.namespaces

    def create_namespace(self, name: str) -> None:
        if name not in self.namespaces:
            self.namespaces[name] = {"vlans": []}

    def delete_namespace(self, name: str) -> None:
        self.namespaces.pop(name, None)

    def attach_vlan(
        self,
        namespace: str,
        parent_if: str,
        vlan_id: int,
        ip_cidr: str,
        gateway: Optional[str] = None,
        mtu: int = 1500,
        ifname: Optional[str] = None,
    ) -> str:
        resolved = ifname or f"v{vlan_id}-fake"
        if namespace not in self.namespaces:
            raise RuntimeError(f"Namespace {namespace} does not exist")
        self.namespaces[namespace]["vlans"].append(
            {"ifname": resolved, "vlan_id": vlan_id, "ip_cidr": ip_cidr, "gateway": gateway}
        )
        return resolved
