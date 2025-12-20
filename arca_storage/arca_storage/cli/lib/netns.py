"""
Network Namespace management functions.
"""

import shlex
import subprocess
from typing import Optional


def create_namespace(name: str) -> None:
    """
    Create a network namespace.
    
    Args:
        name: Namespace name
        
    Raises:
        RuntimeError: If namespace creation fails
    """
    # Check if namespace already exists
    result = subprocess.run(
        ["ip", "netns", "list"],
        capture_output=True,
        text=True,
        check=False
    )
    
    if name in result.stdout:
        # Namespace already exists, skip
        return
    
    # Create namespace
    result = subprocess.run(
        ["ip", "netns", "add", name],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create namespace {name}: {result.stderr}")


def attach_vlan(
    namespace: str,
    parent_if: str,
    vlan_id: int,
    ip_cidr: str,
    gateway: Optional[str] = None,
    mtu: int = 1500
) -> None:
    """
    Create a VLAN interface and attach it to a namespace.
    
    Args:
        namespace: Namespace name
        parent_if: Parent interface (e.g., "bond0")
        vlan_id: VLAN ID
        ip_cidr: IP address with CIDR (e.g., "192.168.10.5/24")
        gateway: Optional gateway IP address
        mtu: MTU size (default: 1500)
        
    Raises:
        RuntimeError: If VLAN attachment fails
    """
    vlan_if = f"{parent_if}.{vlan_id}"
    
    # Check if VLAN interface already exists
    result = subprocess.run(
        ["ip", "link", "show", vlan_if],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode == 0:
        # Interface exists, check if it's in the namespace
        result = subprocess.run(
            ["ip", "netns", "exec", namespace, "ip", "link", "show", vlan_if],
            capture_output=True,
            text=True,
            check=False
        )
        
        if result.returncode == 0:
            # Already in namespace, just configure IP
            _configure_ip(namespace, vlan_if, ip_cidr, gateway, mtu)
            return
        else:
            # Move to namespace
            subprocess.run(
                ["ip", "link", "set", vlan_if, "netns", namespace],
                check=True
            )
    else:
        # Create VLAN interface
        subprocess.run(
            ["ip", "link", "add", "link", parent_if, "name", vlan_if, "type", "vlan", "id", str(vlan_id)],
            check=True
        )
        
        # Move to namespace
        subprocess.run(
            ["ip", "link", "set", vlan_if, "netns", namespace],
            check=True
        )
    
    # Configure IP and bring up
    _configure_ip(namespace, vlan_if, ip_cidr, gateway, mtu)


def _configure_ip(
    namespace: str,
    interface: str,
    ip_cidr: str,
    gateway: Optional[str],
    mtu: int
) -> None:
    """Configure IP address and gateway in namespace."""
    # Set MTU if not default
    if mtu != 1500:
        subprocess.run(
            ["ip", "netns", "exec", namespace, "ip", "link", "set", interface, "mtu", str(mtu)],
            check=True
        )
    
    # Check if IP is already configured
    result = subprocess.run(
        ["ip", "netns", "exec", namespace, "ip", "addr", "show", interface],
        capture_output=True,
        text=True,
        check=False
    )
    
    if ip_cidr not in result.stdout:
        # Add IP address
        subprocess.run(
            ["ip", "netns", "exec", namespace, "ip", "addr", "add", ip_cidr, "dev", interface],
            check=True
        )
    
    # Bring interface up
    subprocess.run(
        ["ip", "netns", "exec", namespace, "ip", "link", "set", interface, "up"],
        check=True
    )
    
    # Configure gateway if provided
    if gateway:
        # Remove existing default route
        subprocess.run(
            ["ip", "netns", "exec", namespace, "ip", "route", "del", "default"],
            capture_output=True,
            check=False
        )
        
        # Add default route
        subprocess.run(
            ["ip", "netns", "exec", namespace, "ip", "route", "add", "default", "via", gateway],
            check=True
        )


def delete_namespace(name: str) -> None:
    """
    Delete a network namespace.
    
    Args:
        name: Namespace name
        
    Raises:
        RuntimeError: If namespace deletion fails
    """
    # Check if namespace exists
    result = subprocess.run(
        ["ip", "netns", "list"],
        capture_output=True,
        text=True,
        check=False
    )
    
    if name not in result.stdout:
        # Namespace doesn't exist, skip
        return
    
    # Delete namespace (this also removes all interfaces in it)
    result = subprocess.run(
        ["ip", "netns", "del", name],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to delete namespace {name}: {result.stderr}")

