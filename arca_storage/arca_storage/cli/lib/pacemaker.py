"""
Pacemaker resource management functions.
"""

import re
import subprocess
from typing import Optional, Sequence

from arca_storage.cli.lib.netns import make_vlan_ifname

_DEFAULT_COMMAND_TIMEOUT_SECONDS = 30
_RESOURCE_ATTR_RE = re.compile(
    r"""(?:^|\s)(?P<name>[\w-]+)=(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>\S+))"""
)


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        check=False,
        timeout=_DEFAULT_COMMAND_TIMEOUT_SECONDS,
    )


def _resource_exists(name: str) -> bool:
    return _run(["pcs", "resource", "show", name]).returncode == 0


def _constraints_text() -> str:
    result = _run(["pcs", "constraint", "show", "--full"])
    return (result.stdout or "") + "\n" + (result.stderr or "")


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
            return (
                match.group("double")
                or match.group("single")
                or match.group("bare")
                or ""
            )
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


def _group_members(group_name: str) -> list[str]:
    result = _run(["pcs", "resource", "config", group_name])
    if result.returncode != 0:
        result = _run(["pcs", "resource", "show", group_name])
    return _parse_group_members(
        group_name, (result.stdout or "") + "\n" + (result.stderr or "")
    )


def _resource_text(name: str) -> str:
    result = _run(["pcs", "resource", "config", name])
    if result.returncode != 0:
        result = _run(["pcs", "resource", "show", name])
    return (result.stdout or "") + "\n" + (result.stderr or "")


def _ensure_resource_attribute(resource: str, name: str, value: str) -> None:
    if _parse_resource_attribute(_resource_text(resource), name) == value:
        return
    result = _run(["pcs", "resource", "update", resource, f"{name}={value}"])
    if result.returncode != 0:
        raise RuntimeError("Failed to update Pacemaker resource attribute")


def _ensure_group_members(group_name: str, resources: list[str]) -> None:
    members = _group_members(group_name)
    for index, resource in enumerate(resources):
        if _group_member_is_ordered(resource, resources, index, members):
            continue
        command, before, after = _group_add_command(
            group_name, resource, resources, index, members
        )
        result = _run(command)
        if result.returncode != 0:
            raise RuntimeError("Failed to add Pacemaker resource to group")
        members = _insert_group_member(members, resource, before=before, after=after)


def _previous_desired_member(
    resources: list[str], index: int, members: list[str]
) -> Optional[str]:
    for resource in reversed(resources[:index]):
        if resource in members:
            return resource
    return None


def _next_desired_member(
    resources: list[str], index: int, members: list[str]
) -> Optional[str]:
    for resource in resources[index + 1 :]:
        if resource in members:
            return resource
    return None


def _group_member_is_ordered(
    resource: str, resources: list[str], index: int, members: list[str]
) -> bool:
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
        return (
            [
                "pcs",
                "resource",
                "group",
                "add",
                group_name,
                resource,
                "--after",
                previous,
            ],
            None,
            previous,
        )

    next_member = _next_desired_member(resources, index, members)
    if next_member is not None:
        return (
            [
                "pcs",
                "resource",
                "group",
                "add",
                group_name,
                resource,
                "--before",
                next_member,
            ],
            next_member,
            None,
        )
    if previous is not None:
        return (
            [
                "pcs",
                "resource",
                "group",
                "add",
                group_name,
                resource,
                "--after",
                previous,
            ],
            None,
            previous,
        )
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


def ensure_drbd_master(drbd_resource_name: str = "r0") -> str:
    """
    Ensure DRBD resource and master/clone are created in Pacemaker.

    Returns:
        Master resource name (e.g., "ms_drbd_r0")
    """
    primitive = f"p_drbd_{drbd_resource_name}"
    master = f"ms_drbd_{drbd_resource_name}"

    if not _resource_exists(primitive):
        result = _run(
            [
                "pcs",
                "resource",
                "create",
                primitive,
                "ocf:linbit:drbd",
                f"drbd_resource={drbd_resource_name}",
                "op",
                "monitor",
                "interval=15s",
                "role=Master",
            ]
        )
        if result.returncode != 0:
            raise RuntimeError("Failed to create DRBD resource")

    if not _resource_exists(master):
        result = _run(
            [
                "pcs",
                "resource",
                "master",
                master,
                primitive,
                "master-max=1",
                "master-node-max=1",
                "clone-max=2",
                "clone-node-max=1",
            ]
        )
        if result.returncode != 0:
            raise RuntimeError("Failed to create DRBD master resource")

    return master


def ensure_order(master_name: str, target_resource: str) -> None:
    """
    Ensure order constraint: <master>:promote then <target>:start
    """
    needle = f"order {master_name}:promote {target_resource}:start"
    if needle in _constraints_text():
        return
    result = _run(
        [
            "pcs",
            "constraint",
            "order",
            f"{master_name}:promote",
            f"{target_resource}:start",
        ]
    )
    if result.returncode != 0:
        raise RuntimeError("Failed to create Pacemaker order constraint")


def ensure_colocation(group_name: str, master_name: str) -> None:
    """
    Ensure colocation: <group> with <master>:Master
    """
    needle = f"colocation {group_name} with {master_name}:Master"
    if needle in _constraints_text():
        return
    result = _run(
        [
            "pcs",
            "constraint",
            "colocation",
            "add",
            group_name,
            "with",
            f"{master_name}:Master",
        ]
    )
    if result.returncode != 0:
        raise RuntimeError("Failed to create Pacemaker colocation constraint")


def create_group(
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
    """
    Create a Pacemaker resource group for an SVM.

    Args:
        svm_name: SVM name
        mount_path: Filesystem mount path
        vlan_id: Optional VLAN ID (1-4094). If omitted, an IPaddr2 resource
            is created on parent_if and Ganesha runs in the host namespace.
        ip: VIP IP address (without prefix)
        prefix: Prefix length (e.g., 24)
        gw: Gateway IP (required)
        mtu: MTU size
        parent_if: Parent interface (default: bond0)
        vg_name: Volume group name for Filesystem resource device path
        filesystem_lv_name: Optional logical volume name for the Filesystem resource.
        create_filesystem: Whether to create Filesystem resource (default: True)

    Raises:
        RuntimeError: If resource group creation fails
    """
    group_name = f"g_svm_{svm_name}"
    group_exists = _resource_exists(group_name)

    resources: list[str] = []

    master_name: Optional[str] = None
    if enforce_drbd_constraints:
        master_name = ensure_drbd_master(drbd_resource_name)

    # Create Filesystem resource (optional)
    fs_resource = f"fs_{svm_name}"
    if create_filesystem:
        device = f"/dev/{vg_name}/{filesystem_lv_name or f'vol_{svm_name}'}"
        if not _resource_exists(fs_resource):
            result = _run(
                [
                    "pcs",
                    "resource",
                    "create",
                    fs_resource,
                    "ocf:heartbeat:Filesystem",
                    f"device={device}",
                    f"directory={mount_path}",
                    "fstype=xfs",
                    "op",
                    "monitor",
                    "interval=10s",
                ]
            )
            if result.returncode != 0:
                raise RuntimeError("Failed to create Filesystem resource")
        elif filesystem_lv_name:
            _ensure_resource_attribute(fs_resource, "device", device)
        resources.append(fs_resource)

    if vlan_id is None:
        ip_resource = f"ip_{svm_name}"
        if not _resource_exists(ip_resource):
            result = _run(
                [
                    "pcs",
                    "resource",
                    "create",
                    ip_resource,
                    "ocf:heartbeat:IPaddr2",
                    f"ip={ip}",
                    f"cidr_netmask={prefix}",
                    f"nic={parent_if}",
                    "op",
                    "monitor",
                    "interval=10s",
                ]
            )
            if result.returncode != 0:
                raise RuntimeError("Failed to create IPaddr2 resource")
        resources.append(ip_resource)
        ganesha_unit = "nfs-ganesha-host"
    else:
        # Create NetnsVlan resource
        netns_resource = f"netns_{svm_name}"
        if not _resource_exists(netns_resource):
            resolved_ifname = ifname or make_vlan_ifname(svm_name, vlan_id)
            cmd = [
                "pcs",
                "resource",
                "create",
                netns_resource,
                "ocf:local:NetnsVlan",
                f"ns={svm_name}",
                f"vlan_id={vlan_id}",
                f"parent_if={parent_if}",
                f"ifname={resolved_ifname}",
                f"ip={ip}",
                f"prefix={prefix}",
            ]
            cmd.append(f"gw={gw}")
            cmd.append(f"mtu={mtu}")
            cmd += ["op", "monitor", "interval=10s"]
            result = _run(cmd)
            if result.returncode != 0:
                raise RuntimeError("Failed to create NetnsVlan resource")
        resources.append(netns_resource)
        ganesha_unit = "nfs-ganesha"

    # Create nfs-ganesha resource
    ganesha_resource = f"ganesha_{svm_name}"
    if not _resource_exists(ganesha_resource):
        result = _run(
            [
                "pcs",
                "resource",
                "create",
                ganesha_resource,
                f"systemd:{ganesha_unit}@{svm_name}",
                "op",
                "monitor",
                "interval=10s",
            ]
        )
        if result.returncode != 0:
            raise RuntimeError("Failed to create NFS-Ganesha resource")
    resources.append(ganesha_resource)

    if not group_exists:
        result = _run(["pcs", "resource", "group", "add", group_name, *resources])
        if result.returncode != 0:
            raise RuntimeError("Failed to create resource group")
    else:
        _ensure_group_members(group_name, resources)

    # Constraints (DRBD -> group/fs ordering, group colocation with DRBD master)
    if master_name:
        # Prefer ordering on filesystem if present, otherwise on first resource in group.
        target = (
            fs_resource
            if (create_filesystem and fs_resource in resources)
            else resources[0]
        )
        ensure_order(master_name, target)
        ensure_colocation(group_name, master_name)


def delete_group(svm_name: str) -> None:
    """
    Delete a Pacemaker resource group for an SVM.

    Args:
        svm_name: SVM name

    Raises:
        RuntimeError: If resource group deletion fails
    """
    group_name = f"g_svm_{svm_name}"

    # Check if group exists
    if not _resource_exists(group_name):
        # Group doesn't exist, skip
        return

    # Stop and delete group
    _run(["pcs", "resource", "disable", group_name])
    result = _run(["pcs", "resource", "delete", group_name])

    if result.returncode != 0:
        raise RuntimeError("Failed to delete resource group")
