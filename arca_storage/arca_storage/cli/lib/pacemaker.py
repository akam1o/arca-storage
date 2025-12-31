"""
Pacemaker resource management functions.
"""

import subprocess
from typing import Optional, Sequence


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)


def _resource_exists(name: str) -> bool:
    return _run(["pcs", "resource", "show", name]).returncode == 0


def _constraints_text() -> str:
    result = _run(["pcs", "constraint", "show", "--full"])
    return (result.stdout or "") + "\n" + (result.stderr or "")


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
            raise RuntimeError(f"Failed to create DRBD resource: {result.stderr.strip()}")

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
            raise RuntimeError(f"Failed to create DRBD master resource: {result.stderr.strip()}")

    return master


def ensure_order(master_name: str, target_resource: str) -> None:
    """
    Ensure order constraint: <master>:promote then <target>:start
    """
    needle = f"order {master_name}:promote {target_resource}:start"
    if needle in _constraints_text():
        return
    result = _run(["pcs", "constraint", "order", f"{master_name}:promote", f"{target_resource}:start"])
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create order constraint: {result.stderr.strip()}")


def ensure_colocation(group_name: str, master_name: str) -> None:
    """
    Ensure colocation: <group> with <master>:Master
    """
    needle = f"colocation {group_name} with {master_name}:Master"
    if needle in _constraints_text():
        return
    result = _run(["pcs", "constraint", "colocation", "add", group_name, "with", f"{master_name}:Master"])
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create colocation constraint: {result.stderr.strip()}")


def create_group(
    svm_name: str,
    mount_path: str,
    *,
    vlan_id: int,
    ip: str,
    prefix: int,
    gw: Optional[str] = None,
    mtu: int = 1500,
    parent_if: str = "bond0",
    vg_name: str = "vg_pool_01",
    create_filesystem: bool = True,
    drbd_resource_name: str = "r0",
    enforce_drbd_constraints: bool = True,
) -> None:
    """
    Create a Pacemaker resource group for an SVM.
    
    Args:
        svm_name: SVM name
        mount_path: Filesystem mount path
        vlan_id: VLAN ID (1-4094)
        ip: VIP IP address (without prefix)
        prefix: Prefix length (e.g., 24)
        gw: Optional default gateway
        mtu: MTU size
        parent_if: Parent interface (default: bond0)
        vg_name: Volume group name for Filesystem resource device path
        create_filesystem: Whether to create Filesystem resource (default: True)
        
    Raises:
        RuntimeError: If resource group creation fails
    """
    group_name = f"g_svm_{svm_name}"
    
    # Check if group already exists
    if _resource_exists(group_name):
        # Group already exists, skip
        return

    resources: list[str] = []

    master_name: Optional[str] = None
    if enforce_drbd_constraints:
        master_name = ensure_drbd_master(drbd_resource_name)

    # Create Filesystem resource (optional)
    fs_resource = f"fs_{svm_name}"
    if create_filesystem and not _resource_exists(fs_resource):
        device = f"/dev/{vg_name}/vol_{svm_name}"
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
            raise RuntimeError(f"Failed to create Filesystem resource: {result.stderr.strip()}")
        resources.append(fs_resource)

    # Create NetnsVlan resource
    netns_resource = f"netns_{svm_name}"
    if not _resource_exists(netns_resource):
        cmd = [
            "pcs",
            "resource",
            "create",
            netns_resource,
            "ocf:local:NetnsVlan",
            f"ns={svm_name}",
            f"vlan_id={vlan_id}",
            f"parent_if={parent_if}",
            f"ip={ip}",
            f"prefix={prefix}",
        ]
        if gw:
            cmd.append(f"gw={gw}")
        cmd.append(f"mtu={mtu}")
        cmd += ["op", "monitor", "interval=10s"]
        result = _run(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create NetnsVlan resource: {result.stderr.strip()}")
    resources.append(netns_resource)

    # Create nfs-ganesha resource
    ganesha_resource = f"ganesha_{svm_name}"
    if not _resource_exists(ganesha_resource):
        result = _run(
            [
                "pcs",
                "resource",
                "create",
                ganesha_resource,
                f"systemd:nfs-ganesha@{svm_name}",
                "op",
                "monitor",
                "interval=10s",
            ]
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create NFS-Ganesha resource: {result.stderr.strip()}")
    resources.append(ganesha_resource)

    # Create resource group
    result = _run(["pcs", "resource", "group", "add", group_name, *resources])
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create resource group: {result.stderr.strip()}")

    # Constraints (DRBD -> group/fs ordering, group colocation with DRBD master)
    if master_name:
        # Prefer ordering on filesystem if present, otherwise on first resource in group.
        target = fs_resource if (create_filesystem and fs_resource in resources) else resources[0]
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
        raise RuntimeError(f"Failed to delete resource group: {result.stderr.strip()}")
