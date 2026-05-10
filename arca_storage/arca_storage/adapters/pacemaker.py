"""
Pacemaker resource management adapter.
"""

from __future__ import annotations

import re
from typing import Optional, Protocol, runtime_checkable

from arca_storage.adapters._subprocess import run_cmd
from arca_storage.cli.lib.netns import make_vlan_ifname

_RESOURCE_ATTR_RE = re.compile(
    r"""(?:^|\s)(?P<name>[\w-]+)=(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>\S+))"""
)


@runtime_checkable
class PacemakerAdapter(Protocol):
    def resource_exists(self, name: str) -> bool: ...

    def create_group(
        self,
        svm_name: str,
        mount_path: str,
        *,
        vlan_id: Optional[int],
        ifname: Optional[str],
        ip: str,
        prefix: int,
        gw: Optional[str],
        mtu: int,
        parent_if: str,
        vg_name: str,
        filesystem_lv_name: Optional[str],
        create_filesystem: bool,
        drbd_resource_name: str,
        enforce_drbd_constraints: bool,
    ) -> None: ...

    def delete_group(self, svm_name: str) -> None: ...


class SubprocessPacemakerAdapter:
    """Production adapter — calls pcs commands."""

    def __init__(self, timeout: int = 60) -> None:
        self._timeout = timeout

    def resource_exists(self, name: str) -> bool:
        result = run_cmd(["pcs", "resource", "show", name], timeout=self._timeout, check=False)
        return result.returncode == 0

    def create_group(
        self,
        svm_name: str,
        mount_path: str,
        *,
        vlan_id: Optional[int],
        ifname: Optional[str] = None,
        ip: str,
        prefix: int,
        gw: Optional[str],
        mtu: int = 1500,
        parent_if: str = "bond0",
        vg_name: str = "vg_pool_01",
        filesystem_lv_name: Optional[str] = None,
        create_filesystem: bool = True,
        drbd_resource_name: str = "r0",
        enforce_drbd_constraints: bool = True,
    ) -> None:
        group_name = f"g_svm_{svm_name}"
        group_exists = self.resource_exists(group_name)

        resources: list[str] = []
        master_name: Optional[str] = None

        if enforce_drbd_constraints:
            master_name = self._ensure_drbd_master(drbd_resource_name)

        # Filesystem resource
        fs_resource = f"fs_{svm_name}"
        if create_filesystem:
            device = f"/dev/{vg_name}/{filesystem_lv_name or f'vol_{svm_name}'}"
            if not self.resource_exists(fs_resource):
                run_cmd(
                    [
                        "pcs", "resource", "create", fs_resource,
                        "ocf:heartbeat:Filesystem",
                        f"device={device}", f"directory={mount_path}", "fstype=xfs",
                        "op", "monitor", "interval=10s",
                    ],
                    timeout=self._timeout,
                )
            elif filesystem_lv_name:
                self._ensure_resource_attribute(fs_resource, "device", device)
            resources.append(fs_resource)

        if vlan_id is None:
            ip_resource = f"ip_{svm_name}"
            if not self.resource_exists(ip_resource):
                run_cmd(
                    [
                        "pcs", "resource", "create", ip_resource,
                        "ocf:heartbeat:IPaddr2",
                        f"ip={ip}", f"cidr_netmask={prefix}", f"nic={parent_if}",
                        "op", "monitor", "interval=10s",
                    ],
                    timeout=self._timeout,
                )
            resources.append(ip_resource)
            ganesha_unit = "nfs-ganesha-host"
        else:
            # NetnsVlan resource
            netns_resource = f"netns_{svm_name}"
            if not self.resource_exists(netns_resource):
                resolved_ifname = ifname or make_vlan_ifname(svm_name, vlan_id)
                run_cmd(
                    [
                        "pcs", "resource", "create", netns_resource,
                        "ocf:local:NetnsVlan",
                        f"ns={svm_name}", f"vlan_id={vlan_id}", f"parent_if={parent_if}",
                        f"ifname={resolved_ifname}", f"ip={ip}", f"prefix={prefix}",
                        f"gw={gw}", f"mtu={mtu}",
                        "op", "monitor", "interval=10s",
                    ],
                    timeout=self._timeout,
                )
            resources.append(netns_resource)
            ganesha_unit = "nfs-ganesha"

        # Ganesha resource
        ganesha_resource = f"ganesha_{svm_name}"
        if not self.resource_exists(ganesha_resource):
            run_cmd(
                [
                    "pcs", "resource", "create", ganesha_resource,
                    f"systemd:{ganesha_unit}@{svm_name}",
                    "op", "monitor", "interval=10s",
                ],
                timeout=self._timeout,
            )
        resources.append(ganesha_resource)

        if not group_exists:
            run_cmd(
                ["pcs", "resource", "group", "add", group_name, *resources],
                timeout=self._timeout,
            )
        else:
            self._ensure_group_members(group_name, resources)

        # Constraints
        if master_name:
            target = fs_resource if (create_filesystem and fs_resource in resources) else resources[0]
            self._ensure_order(master_name, target)
            self._ensure_colocation(group_name, master_name)

    def delete_group(self, svm_name: str) -> None:
        group_name = f"g_svm_{svm_name}"
        if not self.resource_exists(group_name):
            return  # idempotent
        run_cmd(["pcs", "resource", "disable", group_name], timeout=self._timeout, check=False)
        run_cmd(["pcs", "resource", "delete", group_name], timeout=self._timeout)

    # ---- internal helpers ----

    def _ensure_drbd_master(self, drbd_resource_name: str) -> str:
        primitive = f"p_drbd_{drbd_resource_name}"
        master = f"ms_drbd_{drbd_resource_name}"
        if not self.resource_exists(primitive):
            run_cmd(
                [
                    "pcs", "resource", "create", primitive,
                    "ocf:linbit:drbd", f"drbd_resource={drbd_resource_name}",
                    "op", "monitor", "interval=15s", "role=Master",
                ],
                timeout=self._timeout,
            )
        if not self.resource_exists(master):
            run_cmd(
                [
                    "pcs", "resource", "master", master, primitive,
                    "master-max=1", "master-node-max=1", "clone-max=2", "clone-node-max=1",
                ],
                timeout=self._timeout,
            )
        return master

    def _constraints_text(self) -> str:
        result = run_cmd(["pcs", "constraint", "show", "--full"], timeout=self._timeout, check=False)
        return (result.stdout or "") + "\n" + (result.stderr or "")

    def _group_members(self, group_name: str) -> list[str]:
        result = run_cmd(["pcs", "resource", "config", group_name], timeout=self._timeout, check=False)
        if result.returncode != 0:
            result = run_cmd(["pcs", "resource", "show", group_name], timeout=self._timeout, check=False)
        return _parse_group_members(group_name, (result.stdout or "") + "\n" + (result.stderr or ""))

    def _resource_text(self, name: str) -> str:
        result = run_cmd(["pcs", "resource", "config", name], timeout=self._timeout, check=False)
        if result.returncode != 0:
            result = run_cmd(["pcs", "resource", "show", name], timeout=self._timeout, check=False)
        return (result.stdout or "") + "\n" + (result.stderr or "")

    def _ensure_resource_attribute(self, resource: str, name: str, value: str) -> None:
        if _parse_resource_attribute(self._resource_text(resource), name) == value:
            return
        run_cmd(["pcs", "resource", "update", resource, f"{name}={value}"], timeout=self._timeout)

    def _ensure_group_members(self, group_name: str, resources: list[str]) -> None:
        members = self._group_members(group_name)
        for index, resource in enumerate(resources):
            if _group_member_is_ordered(resource, resources, index, members):
                continue
            command, before, after = _group_add_command(group_name, resource, resources, index, members)
            run_cmd(command, timeout=self._timeout)
            members = _insert_group_member(members, resource, before=before, after=after)

    def _ensure_order(self, master_name: str, target: str) -> None:
        needle = f"order {master_name}:promote {target}:start"
        if needle in self._constraints_text():
            return
        run_cmd(
            ["pcs", "constraint", "order", f"{master_name}:promote", f"{target}:start"],
            timeout=self._timeout,
        )

    def _ensure_colocation(self, group_name: str, master_name: str) -> None:
        needle = f"colocation {group_name} with {master_name}:Master"
        if needle in self._constraints_text():
            return
        run_cmd(
            ["pcs", "constraint", "colocation", "add", group_name, "with", f"{master_name}:Master"],
            timeout=self._timeout,
        )


class FakePacemakerAdapter:
    """In-memory fake for testing."""

    def __init__(self) -> None:
        self.resources: dict[str, dict] = {}
        self.groups: dict[str, list[str]] = {}

    def resource_exists(self, name: str) -> bool:
        return name in self.resources or name in self.groups

    def create_group(
        self,
        svm_name: str,
        mount_path: str,
        *,
        vlan_id: Optional[int],
        ifname: Optional[str] = None,
        ip: str,
        prefix: int,
        gw: Optional[str],
        mtu: int = 1500,
        parent_if: str = "bond0",
        vg_name: str = "vg_pool_01",
        filesystem_lv_name: Optional[str] = None,
        create_filesystem: bool = True,
        drbd_resource_name: str = "r0",
        enforce_drbd_constraints: bool = True,
    ) -> None:
        group_name = f"g_svm_{svm_name}"
        group_exists = group_name in self.groups
        resources = []
        if create_filesystem:
            fs = f"fs_{svm_name}"
            device = f"/dev/{vg_name}/{filesystem_lv_name or f'vol_{svm_name}'}"
            resource = self.resources.setdefault(
                fs,
                {
                    "type": "Filesystem",
                    "device": device,
                },
            )
            if filesystem_lv_name:
                resource["device"] = device
            resources.append(fs)
        if vlan_id is None:
            ip_res = f"ip_{svm_name}"
            self.resources.setdefault(ip_res, {"type": "IPaddr2", "ip": ip, "prefix": prefix})
            resources.append(ip_res)
        else:
            netns = f"netns_{svm_name}"
            self.resources.setdefault(netns, {"type": "NetnsVlan"})
            resources.append(netns)
        ganesha = f"ganesha_{svm_name}"
        self.resources.setdefault(ganesha, {"type": "nfs-ganesha-host" if vlan_id is None else "nfs-ganesha"})
        resources.append(ganesha)
        if not group_exists:
            self.groups[group_name] = list(resources)
        else:
            self.groups[group_name] = _reconcile_group_members(self.groups[group_name], resources)

    def delete_group(self, svm_name: str) -> None:
        group_name = f"g_svm_{svm_name}"
        members = self.groups.pop(group_name, [])
        for m in members:
            self.resources.pop(m, None)


def _parse_group_members(group_name: str, text: str) -> list[str]:
    members: list[str] = []
    in_target_group = False
    saw_group_header = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("* "):
            line = line[2:].strip()

        lower = line.lower()
        if line.startswith(f"{group_name}:"):
            for part in line.split(":", 1)[1].split():
                _append_unique(members, part)
            in_target_group = True
            saw_group_header = True
            continue
        if lower.startswith("resource group:") or lower.startswith("group:"):
            header_name = _name_after_colon(line)
            in_target_group = header_name == group_name
            saw_group_header = True
            continue
        if saw_group_header and not in_target_group:
            continue
        if lower.startswith("resource:"):
            _append_unique(members, _name_after_colon(line))
            continue
        if in_target_group and "(" in line:
            _append_unique(members, line.split()[0])
    return members


def _parse_resource_attribute(text: str, name: str) -> Optional[str]:
    for match in _RESOURCE_ATTR_RE.finditer(text):
        if match.group("name") == name:
            return match.group("double") or match.group("single") or match.group("bare") or ""
    return None


def _append_unique(members: list[str], resource: str) -> None:
    resource = resource.strip().rstrip(":")
    if resource and resource not in members:
        members.append(resource)


def _name_after_colon(line: str) -> str:
    parts = line.split(":", 1)
    if len(parts) != 2:
        return ""
    rest = parts[1].strip()
    if not rest:
        return ""
    return rest.split()[0].rstrip(":")


def _previous_desired_member(resources: list[str], index: int, members: list[str]) -> Optional[str]:
    for resource in reversed(resources[:index]):
        if resource in members:
            return resource
    return None


def _next_desired_member(resources: list[str], index: int, members: list[str]) -> Optional[str]:
    for resource in resources[index + 1:]:
        if resource in members:
            return resource
    return None


def _group_member_is_ordered(resource: str, resources: list[str], index: int, members: list[str]) -> bool:
    if resource not in members:
        return False
    resource_index = members.index(resource)

    previous = _previous_desired_member(resources, index, members)
    if previous is not None:
        return members.index(previous) < resource_index

    next_member = _next_desired_member(resources, index, members)
    return next_member is None or resource_index < members.index(next_member)


def _group_add_command(
    group_name: str,
    resource: str,
    resources: list[str],
    index: int,
    members: list[str],
) -> tuple[list[str], Optional[str], Optional[str]]:
    previous = _previous_desired_member(resources, index, members)
    if resource in members and previous is not None:
        return ["pcs", "resource", "group", "add", group_name, resource, "--after", previous], None, previous

    next_member = _next_desired_member(resources, index, members)
    if next_member is not None:
        return ["pcs", "resource", "group", "add", group_name, resource, "--before", next_member], next_member, None
    if previous is not None:
        return ["pcs", "resource", "group", "add", group_name, resource, "--after", previous], None, previous
    return ["pcs", "resource", "group", "add", group_name, resource], None, None


def _insert_group_member(
    members: list[str],
    resource: str,
    *,
    before: Optional[str],
    after: Optional[str],
) -> list[str]:
    updated = [member for member in members if member != resource]
    if before is not None and before in updated:
        updated.insert(updated.index(before), resource)
    elif after is not None and after in updated:
        updated.insert(updated.index(after) + 1, resource)
    else:
        updated.append(resource)
    return updated


def _reconcile_group_members(members: list[str], resources: list[str]) -> list[str]:
    updated = list(members)
    for index, resource in enumerate(resources):
        if _group_member_is_ordered(resource, resources, index, updated):
            continue
        _, before, after = _group_add_command("", resource, resources, index, updated)
        updated = _insert_group_member(updated, resource, before=before, after=after)
    return updated
