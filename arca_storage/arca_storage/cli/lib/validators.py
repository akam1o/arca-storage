"""
Input validation functions.
"""

import ipaddress
import re
from typing import Tuple


LVM_NAME_MAX_LENGTH = 127
_RESOURCE_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*")


def validate_name(name: str) -> None:
    """
    Validate a name (SVM, volume, etc.).
    
    Args:
        name: Name to validate
        
    Raises:
        ValueError: If name is invalid
    """
    if not name:
        raise ValueError("Name cannot be empty")
    
    if len(name) < 1 or len(name) > 64:
        raise ValueError("Name must be between 1 and 64 characters")
    
    # Allow alphanumeric, dots, underscores, hyphens
    if not _RESOURCE_NAME_RE.fullmatch(name):
        raise ValueError("Name must start with alphanumeric and contain only alphanumeric, dots, underscores, or hyphens")


def validate_lvm_name(name: str, *, resource: str = "Logical volume") -> None:
    """Validate an LVM object name derived from API resource names."""
    if len(name) > LVM_NAME_MAX_LENGTH:
        raise ValueError(
            f"{resource} name '{name}' is too long for LVM "
            f"({len(name)} > {LVM_NAME_MAX_LENGTH} characters)"
        )


def svm_root_lv_name(svm: str) -> str:
    name = f"vol_{svm}"
    validate_lvm_name(name, resource="SVM root logical volume")
    return name


def volume_lv_name(svm: str, volume: str) -> str:
    name = f"vol_{svm}_{volume}"
    validate_lvm_name(name, resource="Volume logical volume")
    return name


def snapshot_lv_name(svm: str, volume: str, snapshot: str) -> str:
    name = f"vol_{svm}_{volume}_snap_{snapshot}"
    validate_lvm_name(name, resource="Snapshot logical volume")
    return name


def validate_vlan(vlan_id: int) -> None:
    """
    Validate a VLAN ID.
    
    Args:
        vlan_id: VLAN ID to validate
        
    Raises:
        ValueError: If VLAN ID is invalid
    """
    if vlan_id < 1 or vlan_id > 4094:
        raise ValueError("VLAN ID must be between 1 and 4094")


def validate_ip_cidr(cidr: str) -> Tuple[str, int]:
    """
    Validate an IP address with CIDR notation.
    
    Args:
        cidr: IP address with CIDR (e.g., "192.168.10.5/24")
        
    Returns:
        Tuple of (ip_address, prefix_length)
        
    Raises:
        ValueError: If CIDR is invalid
    """
    try:
        parts = cidr.split("/")
        if len(parts) != 2:
            raise ValueError("CIDR must be in format IP/PREFIX (e.g., 192.168.10.5/24)")
        
        ip_addr = parts[0]
        prefix = int(parts[1])
        
        # Validate IP address
        ipaddress.IPv4Address(ip_addr)
        
        # Validate prefix
        if prefix < 0 or prefix > 32:
            raise ValueError("Prefix length must be between 0 and 32")
        
        return ip_addr, prefix
        
    except ValueError as e:
        raise ValueError(f"Invalid CIDR format: {e}")
    except Exception as e:
        raise ValueError(f"Invalid IP address: {e}")


def validate_svm_ip_cidr(cidr: str) -> Tuple[str, int]:
    """
    Validate an SVM VIP with CIDR notation.

    Unlike generic client CIDRs, an SVM VIP must be a usable unicast host
    address so it can be bound by Ganesha, netns, and Pacemaker resources.
    """
    try:
        iface = ipaddress.IPv4Interface(cidr)
    except Exception as e:
        raise ValueError(f"Invalid CIDR format: {e}")

    ip_addr = iface.ip
    network = iface.network

    if network.prefixlen == 0:
        raise ValueError("SVM IP address prefix must not be /0")
    if ip_addr.is_unspecified:
        raise ValueError("SVM IP address must not be unspecified")
    if ip_addr.is_multicast:
        raise ValueError("SVM IP address must not be multicast")
    if ip_addr.is_loopback:
        raise ValueError("SVM IP address must not be loopback")
    if ip_addr.is_reserved:
        raise ValueError("SVM IP address must not be reserved")
    if network.prefixlen < 31 and ip_addr in (network.network_address, network.broadcast_address):
        raise ValueError("SVM IP address must be a usable host address")

    return str(ip_addr), network.prefixlen


def normalize_ip_cidr(cidr: str) -> str:
    """
    Validate and canonicalize an IPv4 network CIDR.

    Host bits are accepted for compatibility with existing API callers and
    normalized to the containing network.
    """
    try:
        parts = cidr.split("/")
        if len(parts) != 2:
            raise ValueError("CIDR must be in format IP/PREFIX (e.g., 192.168.10.0/24)")
        return str(ipaddress.IPv4Network(cidr, strict=False))
    except Exception as e:
        raise ValueError(f"Invalid CIDR format: {e}")


def validate_ipv4(ip: str) -> None:
    """
    Validate an IPv4 address string.

    Args:
        ip: IPv4 address (e.g., "192.168.10.1")

    Raises:
        ValueError: If IP is invalid
    """
    try:
        ipaddress.IPv4Address(ip)
    except Exception as e:
        raise ValueError(f"Invalid IPv4 address: {e}")


def infer_gateway_from_ip_cidr(cidr: str) -> str:
    """
    Infer a default gateway from an IPv4 interface CIDR.

    Rule:
    - Pick the first usable host address in the subnet that is not equal to the interface IP.
      (e.g., 192.168.10.5/24 -> 192.168.10.1, 192.168.10.1/24 -> 192.168.10.2)

    Notes:
    - /31 and /32 do not have a clear "default gateway" convention in this project, so callers
      must provide an explicit gateway in those cases.
    """
    try:
        iface = ipaddress.IPv4Interface(cidr)
    except Exception as e:
        raise ValueError(f"Invalid CIDR format: {e}")

    if iface.network.prefixlen >= 31:
        raise ValueError("Gateway cannot be inferred for /31 or /32; please specify gateway explicitly")

    ip = iface.ip
    for host in iface.network.hosts():
        if host != ip:
            return str(host)

    raise ValueError("Gateway could not be inferred from CIDR; please specify gateway explicitly")
