"""
systemd unit management functions.
"""

import subprocess


def start_unit(unit_name: str) -> None:
    """
    Start a systemd unit.
    
    Args:
        unit_name: Unit name (e.g., "nfs-ganesha@svm_name")
        
    Raises:
        RuntimeError: If starting unit fails
    """
    result = subprocess.run(
        ["systemctl", "start", unit_name],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to start unit {unit_name}: {result.stderr}")


def stop_unit(unit_name: str) -> None:
    """
    Stop a systemd unit.
    
    Args:
        unit_name: Unit name
        
    Raises:
        RuntimeError: If stopping unit fails
    """
    result = subprocess.run(
        ["systemctl", "stop", unit_name],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to stop unit {unit_name}: {result.stderr}")


def is_active(unit_name: str) -> bool:
    """
    Check if a systemd unit is active.
    
    Args:
        unit_name: Unit name
        
    Returns:
        True if unit is active, False otherwise
    """
    result = subprocess.run(
        ["systemctl", "is-active", unit_name],
        capture_output=True,
        text=True,
        check=False
    )
    
    return result.returncode == 0

