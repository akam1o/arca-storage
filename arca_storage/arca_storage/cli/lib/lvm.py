"""
LVM Thin Provisioning management functions.
"""

import subprocess
from typing import Optional


def create_lv(vg_name: str, lv_name: str, size_gib: int, thin: bool = True, *, thinpool_name: str = "pool") -> str:
    """
    Create a logical volume.
    
    Args:
        vg_name: Volume group name
        lv_name: Logical volume name
        size_gib: Size in GiB
        thin: Use thin provisioning (default: True)
        thinpool_name: Thin pool LV name (default: pool)
        
    Returns:
        Path to the logical volume (e.g., "/dev/vg_name/lv_name")
        
    Raises:
        RuntimeError: If LV creation fails
    """
    lv_path = f"/dev/{vg_name}/{lv_name}"
    
    # Check if LV already exists
    result = subprocess.run(
        ["lvdisplay", lv_path],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode == 0:
        raise RuntimeError(f"Logical volume {lv_path} already exists")
    
    if thin:
        # Create thin volume
        cmd = [
            "lvcreate",
            "-V", f"{size_gib}G",
            "-T", f"{vg_name}/{thinpool_name}",
            "-n", lv_name
        ]
    else:
        # Create regular volume
        cmd = [
            "lvcreate",
            "-L", f"{size_gib}G",
            "-n", lv_name,
            vg_name
        ]
    
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create logical volume: {result.stderr}")
    
    return lv_path


def resize_lv(vg_name: str, lv_name: str, new_size_gib: int) -> None:
    """
    Resize a logical volume.
    
    Args:
        vg_name: Volume group name
        lv_name: Logical volume name
        new_size_gib: New size in GiB
        
    Raises:
        RuntimeError: If LV resize fails
    """
    lv_path = f"/dev/{vg_name}/{lv_name}"
    
    # Check if LV exists
    result = subprocess.run(
        ["lvdisplay", lv_path],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Logical volume {lv_path} does not exist")
    
    # Resize LV
    result = subprocess.run(
        ["lvextend", "-L", f"{new_size_gib}G", lv_path],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to resize logical volume: {result.stderr}")


def delete_lv(vg_name: str, lv_name: str) -> None:
    """
    Delete a logical volume.
    
    Args:
        vg_name: Volume group name
        lv_name: Logical volume name
        
    Raises:
        RuntimeError: If LV deletion fails
    """
    lv_path = f"/dev/{vg_name}/{lv_name}"
    
    # Check if LV exists
    result = subprocess.run(
        ["lvdisplay", lv_path],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        # LV doesn't exist, skip
        return
    
    # Delete LV
    result = subprocess.run(
        ["lvremove", "-f", lv_path],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to delete logical volume: {result.stderr}")
