"""
Network Namespace management functions.
"""

import hashlib
import re
import shlex
import subprocess
from typing import Optional


CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _hash2_base62_sha256(data: bytes) -> str:
    """
    Return 2 base62 characters derived from sha256(data) as:

    value = int.from_bytes(digest, "big")
    c1 = CHARS[value % 62]
    c2 = CHARS[(value // 62) % 62]
    """
    digest = hashlib.sha256(data).digest()
    value = int.from_bytes(digest, "big")
    c1 = CHARS[value % 62]
    c2 = CHARS[(value // 62) % 62]
    return f"{c1}{c2}"


def make_vlan_ifname(svm_name: str, vlan_id: int, *, attempt: int = 0) -> str:
    """
    Generate a deterministic VLAN interface name for an SVM.

    Rationale:
    - Linux interface names are typically limited to 15 chars (IFNAMSIZ-1).
    - Using the traditional "<parent_if>.<vlan_id>" prevents multiple namespaces/SVMs
      from sharing the same VLAN ID because a single interface cannot exist in
      multiple namespaces. A per-SVM name avoids this collision.
    """
    max_len = 15
    prefix = f"v{vlan_id}-"

    # Keep only alphanumerics for portability, and lowercase for consistency.
    safe = re.sub(r"[^a-zA-Z0-9]+", "", svm_name).lower() or "svm"
    seed = f"{svm_name}:{attempt}".encode("utf-8")
    digest = _hash2_base62_sha256(seed)

    # Always reserve the hash suffix to avoid collisions when the shortened
    # SVM name part overlaps across different SVMs.
    if len(prefix) > max_len - len(digest):
        prefix = prefix[: max_len - len(digest)]
    core_len = max_len - len(prefix) - len(digest)
    return (prefix + safe[: max(0, core_len)] + digest)[:max_len]


def _ifname_exists_in_root(ifname: str) -> bool:
    result = subprocess.run(
        ["ip", "link", "show", ifname],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def allocate_vlan_ifname(svm_name: str, vlan_id: int, *, max_attempts: int = 256) -> str:
    """
    Allocate an interface name for the SVM that avoids collisions in the *root*
    namespace at the time of creation.

    Notes:
    - Interface names are per-network-namespace, so the main collision risk is
      concurrent creation in the root namespace before moving the interface into
      the target namespace.
    - This allocator keeps the "v{vlan_id}-<short><hash>" shape but varies the
      hash seed by attempt.
    """
    for attempt in range(max_attempts):
        candidate = make_vlan_ifname(svm_name, vlan_id, attempt=attempt)
        if not _ifname_exists_in_root(candidate):
            return candidate
    raise RuntimeError("Failed to allocate a unique VLAN interface name (too many collisions)")


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
    mtu: int = 1500,
    ifname: Optional[str] = None,
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
    vlan_if = ifname or f"{parent_if}.{vlan_id}"
    
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
