"""Neutron network allocator for VLAN provider networks.

This allocator creates Neutron ports for SVM network interfaces, providing
integration with OpenStack Neutron for IP/VLAN allocation.
"""

import threading

from neutronclient.v2_0 import client as neutron_client
from neutronclient.common import exceptions as neutron_exceptions
from keystoneauth1 import loading as ks_loading
from keystoneauth1 import exceptions as ks_exceptions
from oslo_config import cfg
from oslo_log import log as logging

from ..exceptions import (
    ArcaNetworkConflict,
    ArcaNeutronError,
    ArcaNeutronAuthenticationError,
    ArcaNeutronPortCreationFailed,
)
from .base import NetworkAllocator, NetworkAllocation

LOG = logging.getLogger(__name__)

# Register [neutron] config section (do this once at module level)
NEUTRON_GROUP = 'neutron'
ks_loading.register_session_conf_options(cfg.CONF, NEUTRON_GROUP)
ks_loading.register_auth_conf_options(cfg.CONF, NEUTRON_GROUP)


class NeutronAllocator(NetworkAllocator):
    """Neutron-based network allocator for VLAN provider networks.

    This allocator creates Neutron ports for each SVM, retrieving IP/VLAN
    allocation from Neutron. Only VLAN provider networks are supported.
    """

    # Device owner identifier for ARCA SVM ports
    DEVICE_OWNER = "compute:arca-storage-svm"

    def __init__(self, configuration):
        """Initialize Neutron allocator.

        Args:
            configuration: Driver configuration object
        """
        self.configuration = configuration
        self._neutron_client = None
        self._networks = []  # List of validated network metadata
        self._network_counter = 0  # Counter for round-robin selection
        self._counter_lock = threading.Lock()  # Lock for thread-safe counter increment
        self._supports_tags = False  # Feature detection for tags extension

    def validate_config(self) -> None:
        """Validate Neutron configuration and network requirements.

        This method is idempotent and can be called multiple times safely.

        For each network in the list:
        1. Verify it exists and is a VLAN provider network
        2. Extract VLAN ID from provider:segmentation_id
        3. Auto-detect first subnet and validate gateway_ip
        4. Cache all metadata for allocation

        Raises:
            ValueError: Invalid configuration or unsupported network type
        """
        # Reset state for idempotency
        self._networks = []
        self._neutron_client = None
        self._supports_tags = False
        # Reset counter to ensure consistent behavior on re-validation
        with self._counter_lock:
            self._network_counter = 0

        # Get network IDs from config (handle backward compatibility)
        net_ids = self._get_network_ids_from_config()

        if not net_ids:
            raise ValueError(
                "At least one network required. "
                "Configure arca_storage_neutron_net_ids with comma-separated network UUIDs."
            )

        # Initialize Neutron client using [neutron] section auth
        self._neutron_client = self._create_neutron_client()

        # Validate each network
        for net_id in net_ids:
            network_metadata = self._validate_and_cache_network(net_id)
            self._networks.append(network_metadata)

        # Check for tag extension support (best-effort)
        try:
            extensions = self._neutron_client.list_extensions()["extensions"]
            self._supports_tags = any(ext["alias"] == "tag" for ext in extensions)
            LOG.info("Neutron tag extension: %s", "available" if self._supports_tags else "not available")
        except Exception:
            self._supports_tags = False

        LOG.info(
            "Neutron allocator validated %d network(s): VLANs %s",
            len(self._networks),
            [n["vlan_id"] for n in self._networks]
        )

    def allocate(
        self, project_id: str, svm_name: str, retry_attempt: int = 0
    ) -> NetworkAllocation:
        """Allocate network by creating Neutron Port.

        This method is idempotent: if a port with the expected device_id
        already exists, it will be reused rather than creating a duplicate.

        Uses round-robin selection across multiple networks when available.

        Args:
            project_id: OpenStack project ID
            svm_name: SVM name (for device_id and labeling)
            retry_attempt: Retry attempt number (for network selection randomization)

        Returns:
            NetworkAllocation with VLAN/IP/gateway and port ID

        Raises:
            ArcaNetworkConflict: Port creation failed
        """
        # First check if port already exists for this SVM (idempotency)
        device_id = f"arca-svm-{svm_name}"
        existing_port = self._find_existing_port(device_id)

        if existing_port:
            LOG.info(
                "Reusing existing Neutron port %s for SVM %s",
                existing_port["id"],
                svm_name,
            )
            return self._extract_allocation_from_port(existing_port)

        # Select network using round-robin
        network = self._select_network_round_robin(retry_attempt=retry_attempt)

        # Create new port with selected network
        port_name = f"arca-svm-{svm_name}"

        port_body = {
            "port": {
                "name": port_name,
                "network_id": network["network_id"],
                "fixed_ips": [{
                    "subnet_id": network["subnet_id"]
                }],
                "admin_state_up": True,
                "port_security_enabled": self.configuration.arca_storage_neutron_port_security,
                "device_owner": self.DEVICE_OWNER,
                "device_id": device_id,
            }
        }

        # Add vnic_type (best-effort, depends on binding extension)
        try:
            port_body["port"]["binding:vnic_type"] = (
                self.configuration.arca_storage_neutron_vnic_type
            )
        except Exception:
            LOG.debug("binding:vnic_type not supported, using default")

        # Add tags if supported (best-effort)
        if self._supports_tags:
            port_body["port"]["tags"] = [
                "arca-storage",
                f"svm:{svm_name}",
                f"project:{project_id}",
            ]

        try:
            port = self._neutron_client.create_port(port_body)["port"]
            LOG.info(
                "Created Neutron port %s for SVM %s on network %s (VLAN %d)",
                port["id"],
                svm_name,
                network["network_id"][:8],
                network["vlan_id"],
            )
        except ks_exceptions.Unauthorized as e:
            # Authentication error - not retryable
            LOG.error("Neutron authentication failed: %s", e)
            raise ArcaNeutronAuthenticationError(details=str(e))
        except neutron_exceptions.Unauthorized as e:
            # Neutron client authentication error - not retryable
            LOG.error("Neutron client authentication failed: %s", e)
            raise ArcaNeutronAuthenticationError(details=str(e))
        except ks_exceptions.Forbidden as e:
            # Permission error - not retryable
            LOG.error("Neutron permission denied: %s", e)
            raise ArcaNeutronError(details=f"Permission denied: {e}")
        except neutron_exceptions.Forbidden as e:
            # Neutron client permission error - not retryable
            LOG.error("Neutron client permission denied: %s", e)
            raise ArcaNeutronError(details=f"Permission denied: {e}")
        except neutron_exceptions.BadRequest as e:
            # Invalid request - not retryable
            LOG.error("Invalid Neutron port request: %s", e)
            raise ArcaNeutronPortCreationFailed(details=f"Bad request: {e}")
        except neutron_exceptions.NotFound as e:
            # Resource not found (network/subnet deleted after validation) - not retryable
            LOG.error("Neutron resource not found: %s", e)
            raise ArcaNeutronError(details=f"Resource not found: {e}")
        except neutron_exceptions.Conflict as e:
            # IP conflict or duplicate - retryable
            LOG.warning("Neutron port conflict for SVM %s: %s", svm_name, e)
            raise ArcaNetworkConflict(details=f"Port conflict: {e}")
        except (neutron_exceptions.ServiceUnavailable, neutron_exceptions.ConnectionFailed) as e:
            # Transient error - retryable
            LOG.warning("Neutron service unavailable: %s", e)
            raise ArcaNetworkConflict(details=f"Service unavailable: {e}")
        except Exception as e:
            # Unknown error - treat as retryable but log as error
            LOG.error("Unexpected error creating Neutron port for SVM %s: %s", svm_name, e)
            raise ArcaNetworkConflict(details=f"Failed to create port: {e}")

        # Check for duplicate ports (race condition detection)
        all_ports = self._neutron_client.list_ports(
            device_owner=self.DEVICE_OWNER,
            device_id=device_id,
        )["ports"]

        if len(all_ports) > 1:
            # Duplicate detected - consolidate them
            LOG.warning("Detected %d duplicate ports for device_id %s", len(all_ports), device_id)
            all_ports = self._consolidate_duplicate_ports(all_ports, newly_created_port_id=port["id"])
            port = all_ports[0]
            # CRITICAL FIX: Don't use originally selected network metadata
            # The kept port may be on a different network, so pass network=None
            # to force lookup from port's actual network_id
            network = None

        # Increment counter for next allocation (only on success)
        # Thread-safe increment using lock
        with self._counter_lock:
            self._network_counter += 1

        return self._extract_allocation_from_port(port, network)

    def deallocate(self, allocation_id: str) -> None:
        """Delete Neutron Port.

        Args:
            allocation_id: Neutron Port ID
        """
        if not allocation_id:
            LOG.debug("No allocation_id provided, skipping port deletion")
            return

        try:
            self._neutron_client.delete_port(allocation_id)
            LOG.info("Deleted Neutron port %s", allocation_id)
        except Exception as e:
            LOG.warning("Failed to delete Neutron port %s: %s", allocation_id, e)

    def _consolidate_duplicate_ports(self, ports, newly_created_port_id=None):
        """Consolidate duplicate ports by keeping oldest and deleting the rest.

        Args:
            ports: List of port dicts
            newly_created_port_id: Optional ID of newly created port to prefer

        Returns:
            List containing single port dict that was kept
        """
        # Sort by created_at, with stable fallback for missing timestamps
        # Prefer newly created port (from create_port) if present
        def sort_key(p):
            created_at = p.get("created_at", "")
            # Empty string sorts first, so use explicit ordering:
            # 1. Ports with timestamps (oldest first)
            # 2. Port matching just-created port ID (prefer our port)
            # 3. Other ports without timestamps
            if not created_at:
                # Check if this is the port we just created
                if newly_created_port_id and p["id"] == newly_created_port_id:
                    return ("0", "")  # Prefer our newly created port
                return ("2", p["id"])  # Other ports without timestamp
            return ("1", created_at)  # Normal case: has timestamp

        ports.sort(key=sort_key)
        port_to_keep = ports[0]

        for duplicate_port in ports[1:]:
            try:
                self._neutron_client.delete_port(duplicate_port["id"])
                LOG.info("Deleted duplicate port %s", duplicate_port["id"])
            except Exception as e:
                LOG.error("Failed to delete duplicate port %s: %s", duplicate_port["id"], e)

        return [port_to_keep]

    def _find_existing_port(self, device_id: str):
        """Find existing port by device_id (for idempotency).

        If multiple ports exist (duplicates), automatically consolidates them
        by keeping the oldest and deleting the rest.

        Args:
            device_id: Device ID to search for

        Returns:
            Port dict or None if not found
        """
        try:
            ports = self._neutron_client.list_ports(
                device_owner=self.DEVICE_OWNER,
                device_id=device_id,
            )["ports"]

            if not ports:
                return None

            if len(ports) > 1:
                # Found pre-existing duplicates - consolidate them
                LOG.warning(
                    "Found %d pre-existing duplicate ports for device_id %s, consolidating",
                    len(ports),
                    device_id,
                )
                ports = self._consolidate_duplicate_ports(ports, newly_created_port_id=None)

            return ports[0] if ports else None

        except Exception as e:
            LOG.warning("Failed to query existing ports: %s", e)
        return None

    def _extract_allocation_from_port(self, port, network=None) -> NetworkAllocation:
        """Extract NetworkAllocation from Neutron port.

        Args:
            port: Neutron port dict
            network: Optional network metadata dict (for new allocations).
                    If not provided, will look up network from port's network_id.

        Returns:
            NetworkAllocation with VLAN/IP/gateway
        """
        port_id = port["id"]
        port_network_id = port["network_id"]

        # Validate fixed_ips exists and is not empty
        fixed_ips = port.get("fixed_ips", [])
        if not fixed_ips:
            # This can happen transiently during DHCP allocation
            # Wrap as retryable network conflict rather than hard ValueError
            error_msg = (
                f"Port {port_id} has no fixed IPs. "
                "Port may be misconfigured or still pending IP allocation from DHCP."
            )
            LOG.warning(error_msg)
            raise ArcaNetworkConflict(details=error_msg)

        fixed_ip = fixed_ips[0]
        ip_address = fixed_ip["ip_address"]
        subnet_id = fixed_ip["subnet_id"]

        # Get network metadata (either from parameter or cache lookup)
        if network is None:
            # This is an existing port (idempotency case)
            # Look up network in cached metadata
            network = None
            for net in self._networks:
                if net["network_id"] == port_network_id:
                    network = net
                    break

            if network is None:
                # Network not in cache - this shouldn't happen, but handle gracefully
                LOG.warning(
                    "Port %s references network %s not in validated networks. "
                    "Querying network details from Neutron.",
                    port_id[:8],
                    port_network_id[:8],
                )
                # Fallback: query network from Neutron
                neutron_network = self._neutron_client.show_network(port_network_id)["network"]
                vlan_id = neutron_network.get("provider:segmentation_id")

                # Get subnet for gateway/CIDR from port's actual subnet
                subnet = self._neutron_client.show_subnet(subnet_id)["subnet"]
                gateway = subnet["gateway_ip"]
                cidr_prefix = subnet["cidr"].split("/")[1]
            else:
                # Use cached network metadata for VLAN
                vlan_id = network["vlan_id"]

                # CRITICAL: Use port's actual subnet for gateway/prefix, not cached
                # Networks can have multiple subnets and port may use different one
                subnet = self._neutron_client.show_subnet(subnet_id)["subnet"]
                gateway = subnet["gateway_ip"]
                cidr_prefix = subnet["cidr"].split("/")[1]
        else:
            # New allocation - use provided network metadata
            vlan_id = network["vlan_id"]
            gateway = network["gateway"]
            cidr_prefix = network["cidr_prefix"]

        return NetworkAllocation(
            vlan_id=vlan_id,
            ip_cidr=f"{ip_address}/{cidr_prefix}",
            gateway=gateway,
            allocation_id=port_id,
        )

    def _get_network_ids_from_config(self):
        """Get network IDs from configuration.

        Returns:
            List of network UUIDs

        Raises:
            None - returns empty list if no networks configured
        """
        # Get network IDs from list option
        net_ids = self.configuration.arca_storage_neutron_net_ids

        if net_ids:
            # Strip whitespace and filter empty strings
            cleaned_ids = [net_id.strip() for net_id in net_ids if net_id and net_id.strip()]
            return cleaned_ids

        return []

    def _validate_and_cache_network(self, net_id: str):
        """Validate a single network and return its metadata.

        Args:
            net_id: Network UUID to validate

        Returns:
            Dict with network metadata (network_id, vlan_id, subnet_id, gateway, cidr, cidr_prefix)

        Raises:
            ValueError: Network validation failed
        """
        try:
            # Fetch network details
            network = self._neutron_client.show_network(net_id)["network"]

            # Validate network type is VLAN
            network_type = network.get("provider:network_type")
            if network_type != "vlan":
                raise ValueError(
                    f"Network {net_id} must be a VLAN provider network "
                    f"(got: {network_type}). VXLAN/Geneve networks are not supported."
                )

            # Extract VLAN ID
            vlan_id = network.get("provider:segmentation_id")
            if not vlan_id:
                raise ValueError(f"Network {net_id} missing provider:segmentation_id")

            # Auto-detect subnet - prefer IPv4 with gateway
            subnets = network.get("subnets", [])
            if not subnets:
                raise ValueError(f"Network {net_id} has no subnets")

            # Try to find best IPv4 subnet with gateway
            selected_subnet = None
            for subnet_id in subnets:
                subnet = self._neutron_client.show_subnet(subnet_id)["subnet"]

                # Check if IPv4 (ip_version == 4)
                if subnet.get("ip_version") == 4 and subnet.get("gateway_ip"):
                    selected_subnet = subnet
                    break

            # Fallback to first subnet if no IPv4 with gateway found
            if not selected_subnet:
                LOG.warning(
                    "Network %s: No IPv4 subnet with gateway found, using first subnet %s",
                    net_id[:8], subnets[0][:8]
                )
                subnet_id = subnets[0]
                selected_subnet = self._neutron_client.show_subnet(subnet_id)["subnet"]

            subnet = selected_subnet
            subnet_id = subnet["id"]

            # Validate gateway
            gateway_ip = subnet.get("gateway_ip")
            if not gateway_ip:
                raise ValueError(f"Subnet {subnet_id} must have gateway_ip configured")

            cidr = subnet["cidr"]
            cidr_prefix = cidr.split("/")[1]

            LOG.info(
                "Validated network %s: VLAN %d, subnet %s (%s, gateway %s)",
                net_id[:8], vlan_id, subnet_id[:8], cidr, gateway_ip
            )

            return {
                "network_id": net_id,
                "vlan_id": vlan_id,
                "subnet_id": subnet_id,
                "gateway": gateway_ip,
                "cidr": cidr,
                "cidr_prefix": cidr_prefix,
            }

        except Exception as e:
            LOG.exception("Network validation failed for %s", net_id)
            raise ValueError(f"Network {net_id} validation failed: {e}")

    def _create_neutron_client(self):
        """Create Neutron client using [neutron] section auth configuration.

        This follows OpenStack standard pattern: use keystoneauth1 session
        loading from the [neutron] config group (NOT [keystone_authtoken]).

        Returns:
            Neutron client instance

        Raises:
            ValueError: Neutron authentication not configured
        """
        # Load auth plugin from [neutron] config section
        auth = ks_loading.load_auth_from_conf_options(cfg.CONF, NEUTRON_GROUP)
        if not auth:
            raise ValueError(
                "Neutron authentication not configured. "
                "Please configure [neutron] section in manila.conf with "
                "auth_url, auth_type, username, password, project_name, etc."
            )

        # Create session with loaded auth
        session = ks_loading.load_session_from_conf_options(
            cfg.CONF,
            NEUTRON_GROUP,
            auth=auth,
        )

        # Create Neutron client with session
        return neutron_client.Client(session=session)

    def _select_network_round_robin(self, retry_attempt: int = 0):
        """Select network using round-robin allocation strategy.

        Selects a network from the configured list in round-robin order.
        - Starts at position determined by counter % num_networks
        - For retry attempts, uses randomized offset to avoid conflicts
        - Counter is incremented only after successful port creation (in allocate())

        Note: This method only selects a single network. Port creation failure
        handling happens in allocate() which may retry with a different network.

        Args:
            retry_attempt: Retry attempt number (for randomized offset)

        Returns:
            Dict: Selected network metadata from self._networks

        Raises:
            ValueError: No networks configured
        """
        if not self._networks:
            raise ValueError("No networks configured. Call validate_config() first.")

        num_networks = len(self._networks)

        # Read counter with lock for thread safety
        with self._counter_lock:
            start_idx = self._network_counter % num_networks

        # For retry attempts, use randomized offset like StandaloneAllocator
        if retry_attempt > 0:
            import random
            offset = random.randint(0, num_networks - 1)
            start_idx = (start_idx + offset) % num_networks
            LOG.debug(
                "Retry attempt %d: using randomized offset %d for network selection",
                retry_attempt, offset
            )

        # Select network (always returns first choice in current implementation)
        net_idx = start_idx % num_networks
        network = self._networks[net_idx]

        LOG.debug(
            "Selected network %d/%d: %s (VLAN %d) for allocation (retry=%d)",
            net_idx + 1,
            num_networks,
            network["network_id"][:8],
            network["vlan_id"],
            retry_attempt,
        )

        # Return the selected network
        # Note: Counter is incremented only after successful allocation in allocate()
        return network
