"""Standalone network allocator using static IP pools.

This allocator encapsulates the existing network allocation logic for standalone
(non-Neutron) deployments using statically configured IP/VLAN pools.
"""

import ipaddress
import os
import random
import time
from typing import Any, Dict, List, Set

from oslo_log import log as logging

from ..exceptions import (
    ArcaNetworkConflict,
    ArcaNetworkConfigurationError,
    ArcaNetworkPoolExhausted,
)
from .base import NetworkAllocator, NetworkAllocation

LOG = logging.getLogger(__name__)


class StandaloneAllocator(NetworkAllocator):
    """Standalone network allocator using static IP pools.

    This allocator implements the existing pool-based allocation logic:
    - Round-robin pool selection across multiple pools
    - Sequential IP allocation within pools (with randomization on retry)
    - Collision detection via backend SVM query
    - Retry with random offset on transient conflicts

    Thread Safety Model:
    --------------------
    This allocator is designed to be called WITHIN a lock held by the driver
    (driver._allocation_lock). The lock is per-project, meaning:

    1. Sequential calls for the same project are serialized
    2. Concurrent calls for different projects can race
    3. Races are handled by backend conflict detection + retry

    The allocation_lock parameter is stored for documentation purposes but
    not used internally. All thread synchronization is handled by the driver.

    Error Classification:
    --------------------
    This allocator distinguishes between retryable and non-retryable errors:

    - **Retryable (ArcaNetworkConflict)**: Transient conflicts (race conditions,
      stale data) where retry with different offset may succeed
    - **Non-retryable (ArcaNetworkPoolExhausted)**: Deterministic pool exhaustion
      where retry will never succeed
    - **Non-retryable (ArcaNetworkConfigurationError)**: Invalid configuration
      detected during parsing

    This prevents unnecessary retry loops on deterministic failures.
    """

    def __init__(self, configuration, arca_client, allocation_lock, pool_counter):
        """Initialize standalone allocator.

        Args:
            configuration: Driver configuration object
            arca_client: ARCA API client
            allocation_lock: Threading lock for allocation (held by driver, not used here)
            pool_counter: Shared counter for round-robin pool selection
        """
        self.configuration = configuration
        self.arca_client = arca_client
        self._allocation_lock = allocation_lock  # Held by driver, not used here
        self._pool_allocation_counter = pool_counter
        self._ip_vlan_pools = []

    def validate_config(self) -> None:
        """Parse and validate IP/VLAN pool configuration.

        Raises:
            ArcaNetworkConfigurationError: Invalid configuration
        """
        pool_configs = self.configuration.arca_storage_per_project_ip_pools

        if not pool_configs:
            raise ArcaNetworkConfigurationError(
                details="arca_storage_per_project_ip_pools is required for standalone mode"
            )

        try:
            self._ip_vlan_pools = self._parse_ip_vlan_pools(pool_configs)
        except ValueError as e:
            # Wrap ValueError as ArcaNetworkConfigurationError for explicit classification
            raise ArcaNetworkConfigurationError(details=str(e))

        total_ips = sum(pool["num_hosts"] for pool in self._ip_vlan_pools)
        LOG.info(
            "Standalone allocator initialized: %d pools, %d total IPs",
            len(self._ip_vlan_pools),
            total_ips,
        )

    def allocate(
        self, project_id: str, svm_name: str, retry_attempt: int = 0
    ) -> NetworkAllocation:
        """Allocate network from static pools.

        Args:
            project_id: OpenStack project ID (for logging)
            svm_name: SVM name (for logging)
            retry_attempt: Retry attempt number

        Returns:
            NetworkAllocation with VLAN/IP/gateway

        Raises:
            ArcaNetworkConflict: Transient conflict (retryable)
            ArcaNetworkPoolExhausted: All pools exhausted (non-retryable)
        """
        try:
            vlan_id, ip_cidr, gateway = self._allocate_from_multi_pool(
                project_id, retry_attempt
            )

            return NetworkAllocation(
                vlan_id=vlan_id,
                ip_cidr=ip_cidr,
                gateway=gateway,
                allocation_id=None,  # No cleanup needed for standalone
            )

        except ArcaNetworkPoolExhausted:
            # Re-raise pool exhaustion as-is (non-retryable)
            raise
        except Exception as e:
            # Unknown error - treat as retryable conflict
            LOG.error(
                "Failed to allocate network for project %s (attempt %d): %s",
                project_id,
                retry_attempt,
                e,
            )
            raise ArcaNetworkConflict(
                details=f"Network allocation failed: {str(e)}"
            )

    def deallocate(self, allocation_id: str) -> None:
        """Deallocate network resources.

        For standalone mode, this is a no-op since IPs are reused automatically
        when SVMs are deleted.

        Args:
            allocation_id: Allocation ID (unused)
        """
        pass  # No cleanup needed - IPs are reused automatically

    def _parse_ip_vlan_pools(self, pool_configs: List[str]) -> List[Dict[str, Any]]:
        """Parse and validate IP/VLAN pool configuration.

        Args:
            pool_configs: List of pool config strings
                Format: "ip_cidr|start_ip-end_ip:vlan_id"

        Returns:
            List of parsed pool dicts with keys: ip_network, vlan_id, gateway,
            num_hosts, first_host, last_host

        Raises:
            ValueError: If configuration is invalid
        """
        pools = []

        for i, pool_config in enumerate(pool_configs):
            try:
                # Parse VLAN ID (always at the end after ":")
                parts = pool_config.split(":")
                if len(parts) != 2:
                    raise ValueError(
                        f"Invalid format '{pool_config}'. "
                        f"Expected '<ip_cidr>|<start_ip>-<end_ip>:<vlan_id>'"
                    )

                ip_part, vlan_id_str = parts
                ip_part = ip_part.strip()
                vlan_id_str = vlan_id_str.strip()

                # Parse VLAN ID
                try:
                    vlan_id = int(vlan_id_str)
                except ValueError:
                    raise ValueError(
                        f"Invalid VLAN ID '{vlan_id_str}', must be an integer"
                    )

                if vlan_id < 1 or vlan_id > 4094:
                    raise ValueError(
                        f"VLAN ID {vlan_id} out of range (must be 1-4094)"
                    )

                # IP range is mandatory (must contain "|")
                if "|" not in ip_part:
                    raise ValueError(
                        f"IP range is required. Use format '<ip_cidr>|<start_ip>-<end_ip>:<vlan_id>'. "
                        f"Example: '192.168.100.0/24|192.168.100.10-192.168.100.200:100'"
                    )

                # Parse "ip_cidr|start_ip-end_ip"
                cidr_str, range_str = ip_part.split("|", 1)
                cidr_str = cidr_str.strip()
                range_str = range_str.strip()

                # Parse CIDR
                try:
                    ip_network = ipaddress.ip_network(cidr_str, strict=False)
                except ValueError as e:
                    raise ValueError(f"Invalid IP CIDR '{cidr_str}': {e}")

                # Validate IPv4 only
                if ip_network.version != 4:
                    raise ValueError(
                        f"Only IPv4 pools are supported, got IPv{ip_network.version} "
                        f"in '{pool_config}'"
                    )

                # Parse IP range
                if "-" not in range_str:
                    raise ValueError(
                        f"Invalid IP range '{range_str}'. Expected '<start_ip>-<end_ip>'"
                    )

                start_ip_str, end_ip_str = range_str.split("-", 1)
                start_ip_str = start_ip_str.strip()
                end_ip_str = end_ip_str.strip()

                try:
                    start_ip = ipaddress.ip_address(start_ip_str)
                    end_ip = ipaddress.ip_address(end_ip_str)
                except ValueError as e:
                    raise ValueError(f"Invalid IP address in range: {e}")

                # Validate IPs are IPv4
                if start_ip.version != 4 or end_ip.version != 4:
                    raise ValueError(
                        f"Only IPv4 addresses are supported in range"
                    )

                # Validate IPs are within CIDR
                if start_ip not in ip_network:
                    raise ValueError(
                        f"Start IP {start_ip} is not in CIDR {ip_network}"
                    )
                if end_ip not in ip_network:
                    raise ValueError(
                        f"End IP {end_ip} is not in CIDR {ip_network}"
                    )

                # Validate range order (allow single-IP pools where start == end)
                if start_ip > end_ip:
                    raise ValueError(
                        f"Start IP {start_ip} must be less than or equal to end IP {end_ip}"
                    )

                first_host = start_ip
                last_host = end_ip
                num_hosts = int(end_ip) - int(start_ip) + 1

                if num_hosts <= 0:
                    raise ValueError(
                        f"IP pool has no usable host addresses"
                    )

                # Infer gateway from CIDR (typically first IP in subnet)
                # For most networks: x.x.x.1 is the gateway
                if ip_network.prefixlen == 32:
                    # Single host network, use network address as gateway
                    gateway = str(ip_network.network_address)
                elif ip_network.prefixlen == 31:
                    # Point-to-point, use network address as gateway
                    gateway = str(ip_network.network_address)
                else:
                    # Standard subnet, use first usable IP (.1) as gateway
                    gateway = str(ip_network.network_address + 1)

                # Validate gateway is not in allocatable range
                gateway_ip = ipaddress.ip_address(gateway)
                if gateway_ip >= start_ip and gateway_ip <= end_ip:
                    raise ValueError(
                        f"Gateway IP {gateway} is within allocatable range "
                        f"{start_ip}-{end_ip}. Gateway must be excluded from the range. "
                        f"Example: if gateway is {gateway}, use range like "
                        f"{ip_network.network_address + 2}-{end_ip}"
                    )

                # Validate network/broadcast addresses are not in range
                if ip_network.prefixlen < 31:
                    # For normal subnets, check network and broadcast
                    if start_ip == ip_network.network_address:
                        raise ValueError(
                            f"Network address {ip_network.network_address} cannot be in allocatable range. "
                            f"Start IP must be at least {ip_network.network_address + 1}"
                        )
                    if end_ip == ip_network.broadcast_address:
                        raise ValueError(
                            f"Broadcast address {ip_network.broadcast_address} cannot be in allocatable range. "
                            f"End IP must be at most {ip_network.broadcast_address - 1}"
                        )

                pools.append({
                    "ip_network": ip_network,
                    "vlan_id": vlan_id,
                    "gateway": gateway,
                    "num_hosts": num_hosts,
                    "first_host": first_host,
                    "last_host": last_host,
                })

                LOG.debug(
                    "Parsed pool %d: %s (VLAN %d, %s-%s, %d IPs)",
                    i, str(ip_network), vlan_id, first_host, last_host, num_hosts
                )

            except ValueError as e:
                raise ValueError(
                    f"Invalid pool configuration at index {i} ('{pool_config}'): {e}"
                )

        if not pools:
            raise ValueError(
                "No valid IP/VLAN pools configured for per_project strategy"
            )

        return pools

    def _allocate_from_multi_pool(self, project_id: str, retry_attempt: int = 0) -> tuple:
        """Allocate network from pools using round-robin with collision detection.

        IMPORTANT: This method assumes it's called within the allocation lock
        held by the driver. The counter increment is not separately locked.

        Args:
            project_id: OpenStack project ID
            retry_attempt: Retry attempt number (passed to _find_free_slot_in_pool)

        Returns:
            Tuple of (vlan_id, ip_cidr, gateway)

        Raises:
            ArcaNetworkPoolExhausted: All pools are exhausted (non-retryable)
        """
        # Try round-robin allocation
        num_pools = len(self._ip_vlan_pools)
        start_pool_idx = self._pool_allocation_counter % num_pools

        # Track if all pools were exhausted
        pool_exhaustion_errors = []

        for attempt in range(num_pools):
            pool_idx = (start_pool_idx + attempt) % num_pools
            pool = self._ip_vlan_pools[pool_idx]

            try:
                # Try to allocate from this pool, passing retry attempt for randomization
                vlan_id, ip_cidr = self._find_free_slot_in_pool(pool, attempt=retry_attempt)

                # Success! Increment counter for next allocation
                self._pool_allocation_counter += 1

                gateway = pool["gateway"]

                LOG.debug(
                    "Allocated network for project %s from pool %d: "
                    "VLAN=%d, IP=%s, gateway=%s",
                    project_id, pool_idx, vlan_id, ip_cidr, gateway
                )

                return vlan_id, ip_cidr, gateway

            except PoolExhaustedException as e:
                # This pool is exhausted - track the error and try next pool
                pool_exhaustion_errors.append(str(e))
                LOG.debug("Pool %d exhausted: %s", pool_idx, e)
                continue
            except Exception as e:
                # Unknown error in this pool - log and try next pool
                LOG.warning("Pool %d allocation failed with unexpected error: %s", pool_idx, e)
                continue

        # All pools exhausted or failed - raise non-retryable error
        raise ArcaNetworkPoolExhausted(
            details=(
                f"All {num_pools} IP/VLAN pools exhausted. "
                f"Pool errors: {'; '.join(pool_exhaustion_errors)}"
            )
        )

    def _get_used_ips_in_vlan(self, vlan_id: int) -> Set[ipaddress.IPv4Address]:
        """Get set of all currently used IP addresses in a specific VLAN.

        This method scans ALL SVMs (not just prefix-filtered ones) to detect
        IP conflicts with infrastructure, other Manila backends, or manually
        created SVMs.

        Args:
            vlan_id: VLAN ID to query

        Returns:
            Set of used IP addresses (as ipaddress.IPv4Address objects)
        """
        used_ips = set()

        try:
            svms = self.arca_client.list_svms()

            for svm in svms:
                # Check ALL SVMs in this VLAN, not just prefix-filtered ones
                # This prevents conflicts with infrastructure or other services
                # Ensure vlan_id is int for comparison (API may return string)
                svm_vlan = svm.get("vlan_id")
                try:
                    svm_vlan = int(svm_vlan) if svm_vlan is not None else None
                except (ValueError, TypeError):
                    LOG.warning("Invalid vlan_id type in SVM %s: %s", svm["name"], svm_vlan)
                    continue

                if svm_vlan == vlan_id:
                    # Extract IP from vip or ip_cidr (handle both bare IP and CIDR format)
                    vip = svm.get("vip")
                    ip_cidr = svm.get("ip_cidr")

                    # Try vip first (preferred)
                    if vip:
                        try:
                            # Handle both "1.2.3.4" and "1.2.3.4/24" formats
                            if "/" in str(vip):
                                ip_addr = ipaddress.ip_interface(vip).ip
                            else:
                                ip_addr = ipaddress.ip_address(vip)
                            used_ips.add(ip_addr)
                        except ValueError:
                            LOG.warning("Invalid VIP format in SVM %s: %s", svm["name"], vip)

                    # Fallback to ip_cidr if vip is not available
                    elif ip_cidr:
                        try:
                            ip_addr = ipaddress.ip_interface(ip_cidr).ip
                            used_ips.add(ip_addr)
                        except ValueError:
                            LOG.warning("Invalid ip_cidr format in SVM %s: %s", svm["name"], ip_cidr)

        except Exception as e:
            LOG.warning("Failed to get used IPs in VLAN %d: %s", vlan_id, e)
            # Return empty set and let allocation proceed
            # (will rely on backend to detect conflicts)

        return used_ips

    def _find_free_slot_in_pool(
        self, pool: Dict[str, Any], attempt: int = 0
    ) -> tuple:
        """Find first free IP slot in a pool.

        Args:
            pool: Pool configuration dict
            attempt: Retry attempt number (used to vary starting offset for multi-process races)

        Returns:
            Tuple of (vlan_id, ip_cidr)

        Raises:
            PoolExhaustedException: No free slot found in pool (internal exception)
        """
        ip_network = pool["ip_network"]
        vlan_id = pool["vlan_id"]
        num_hosts = pool["num_hosts"]
        first_host = pool["first_host"]

        # Get all SVMs using this VLAN to find used IPs
        used_ips = self._get_used_ips_in_vlan(vlan_id)

        # On retry attempts, add random offset to avoid repeatedly trying the same IP
        # in multi-process race conditions where list_svms() hasn't caught up
        if attempt > 0:
            # Use process-specific entropy: PID + timestamp + attempt
            # This ensures different processes choose different offsets
            # Use local Random instance to avoid global RNG side effects
            seed = (os.getpid() * 1000000 + int(time.time() * 1000)) ^ attempt
            local_rng = random.Random(seed)
            start_offset = local_rng.randint(0, num_hosts - 1)
            LOG.debug("Retry %d: using random offset (seed based on PID %d)", attempt, os.getpid())
        else:
            start_offset = 0

        # Search for first free IP, starting from potentially randomized offset
        for i in range(num_hosts):
            offset = (start_offset + i) % num_hosts
            ip_addr = first_host + offset

            if ip_addr not in used_ips:
                # Found free slot
                ip_cidr = f"{ip_addr}/{ip_network.prefixlen}"
                LOG.debug(
                    "Found free IP in pool VLAN %d at offset %d (attempt %d)",
                    vlan_id, offset, attempt
                )
                return vlan_id, ip_cidr

        # Pool exhausted - raise internal exception for classification
        raise PoolExhaustedException(
            f"Pool exhausted: VLAN {vlan_id}, all {num_hosts} IP slots used"
        )


class PoolExhaustedException(Exception):
    """Internal exception for pool exhaustion detection.

    This exception is used internally to distinguish pool exhaustion from
    other failures, allowing proper error classification at the allocator level.
    """
    pass
