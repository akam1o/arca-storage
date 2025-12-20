"""
XFS filesystem management functions.
"""

import os
import subprocess
from typing import List, Optional


def format_xfs(device: str, options: Optional[List[str]] = None) -> None:
    """
    Format a device with XFS filesystem.
    
    Args:
        device: Device path (e.g., "/dev/vg_name/lv_name")
        options: Additional mkfs.xfs options
        
    Raises:
        RuntimeError: If formatting fails
    """
    # Check if device exists
    if not os.path.exists(device):
        raise RuntimeError(f"Device {device} does not exist")
    
    # Check if already formatted
    result = subprocess.run(
        ["blkid", device],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode == 0 and "xfs" in result.stdout.lower():
        # Already formatted with XFS, skip
        return
    
    # Default XFS format options from SPEC.md
    cmd = [
        "mkfs.xfs",
        "-b", "size=4096",
        "-m", "crc=1,finobt=1",
        "-i", "size=512,maxpct=25",
        "-d", "agcount=32,su=256k,sw=1"
    ]
    
    if options:
        cmd.extend(options)
    
    cmd.append(device)
    
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to format XFS: {result.stderr}")


def mount_xfs(device: str, mount_point: str) -> None:
    """
    Mount an XFS filesystem.
    
    Args:
        device: Device path
        mount_point: Mount point directory
        
    Raises:
        RuntimeError: If mounting fails
    """
    # Create mount point if it doesn't exist
    os.makedirs(mount_point, exist_ok=True)
    
    # Check if already mounted
    result = subprocess.run(
        ["mountpoint", "-q", mount_point],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode == 0:
        # Already mounted, skip
        return
    
    # Mount options from SPEC.md
    mount_options = [
        "rw",
        "noatime",
        "nodiratime",
        "logbsize=256k",
        "inode64"
    ]
    
    result = subprocess.run(
        ["mount", "-o", ",".join(mount_options), device, mount_point],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to mount XFS: {result.stderr}")


def umount_xfs(mount_point: str) -> None:
    """
    Unmount an XFS filesystem.
    
    Args:
        mount_point: Mount point directory
        
    Raises:
        RuntimeError: If unmounting fails
    """
    # Check if mounted
    result = subprocess.run(
        ["mountpoint", "-q", mount_point],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        # Not mounted, skip
        return
    
    result = subprocess.run(
        ["umount", mount_point],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to unmount XFS: {result.stderr}")


def grow_xfs(mount_point: str) -> None:
    """
    Grow an XFS filesystem (after LV resize).
    
    Args:
        mount_point: Mount point directory
        
    Raises:
        RuntimeError: If growing fails
    """
    # Check if mounted
    result = subprocess.run(
        ["mountpoint", "-q", mount_point],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Mount point {mount_point} is not mounted")
    
    # Grow filesystem
    result = subprocess.run(
        ["xfs_growfs", mount_point],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to grow XFS: {result.stderr}")

