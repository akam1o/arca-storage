"""
Pacemaker resource management functions.
"""

import subprocess
from typing import Optional


def create_group(svm_name: str, mount_path: str, namespace: str, unit_name: str) -> None:
    """
    Create a Pacemaker resource group for an SVM.
    
    Args:
        svm_name: SVM name
        mount_path: Filesystem mount path
        namespace: Network namespace name
        unit_name: systemd unit name (e.g., "nfs-ganesha@svm_name")
        
    Raises:
        RuntimeError: If resource group creation fails
    """
    group_name = f"g_svm_{svm_name}"
    
    # Check if group already exists
    result = subprocess.run(
        ["pcs", "resource", "show", group_name],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode == 0:
        # Group already exists, skip
        return
    
    # Create Filesystem resource
    fs_resource = f"fs_{svm_name}"
    device = f"/dev/vg_pool_01/vol_{svm_name}"  # TODO: Make this configurable
    
    result = subprocess.run(
        [
            "pcs", "resource", "create", fs_resource,
            "ocf:heartbeat:Filesystem",
            f"device={device}",
            f"directory={mount_path}",
            "fstype=xfs",
            "op", "monitor", "interval=10s"
        ],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create Filesystem resource: {result.stderr}")
    
    # Create NetnsVlan resource
    netns_resource = f"netns_{svm_name}"
    # Extract IP and prefix from namespace (TODO: get from state)
    # For now, assuming these are passed or stored somewhere
    # This is a placeholder - actual implementation would need to store/retrieve these values
    
    # Create nfs-ganesha resource
    ganesha_resource = f"ganesha_{svm_name}"
    
    result = subprocess.run(
        [
            "pcs", "resource", "create", ganesha_resource,
            "systemd:nfs-ganesha@",
            f"instance={svm_name}",
            "op", "monitor", "interval=10s"
        ],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create NFS-Ganesha resource: {result.stderr}")
    
    # Create resource group
    result = subprocess.run(
        [
            "pcs", "resource", "group", "add", group_name,
            fs_resource,
            netns_resource,
            ganesha_resource
        ],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create resource group: {result.stderr}")


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
    result = subprocess.run(
        ["pcs", "resource", "show", group_name],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        # Group doesn't exist, skip
        return
    
    # Stop and delete group
    result = subprocess.run(
        ["pcs", "resource", "disable", group_name],
        capture_output=True,
        text=True,
        check=False
    )
    
    result = subprocess.run(
        ["pcs", "resource", "delete", group_name],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to delete resource group: {result.stderr}")

