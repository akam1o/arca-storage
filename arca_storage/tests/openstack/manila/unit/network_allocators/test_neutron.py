"""Unit tests for NeutronAllocator."""

from unittest.mock import Mock, patch, MagicMock

import pytest

from arca_storage.openstack.manila import exceptions as arca_exceptions
from arca_storage.openstack.manila.network_allocators.neutron import NeutronAllocator
from arca_storage.openstack.manila.network_allocators.base import NetworkAllocation


class TestNeutronAllocator:
    """Tests for NeutronAllocator."""

    @pytest.fixture
    def mock_config(self):
        """Create mock configuration."""
        config = Mock()
        # Use list format (even for single network)
        config.arca_storage_neutron_net_ids = ["net-uuid-123"]
        config.arca_storage_neutron_port_security = False
        config.arca_storage_neutron_vnic_type = "normal"
        return config

    @pytest.fixture
    def mock_neutron_client(self):
        """Create mock Neutron client."""
        client = Mock()

        # Mock network with VLAN provider type
        client.show_network.return_value = {
            "network": {
                "id": "net-uuid-123",
                "name": "provider-net",
                "provider:network_type": "vlan",
                "provider:segmentation_id": 100,
                "subnets": ["subnet-uuid-456"],  # Required for auto subnet detection
            }
        }

        # Mock subnet with gateway
        client.show_subnet.return_value = {
            "subnet": {
                "id": "subnet-uuid-456",
                "cidr": "192.168.100.0/24",
                "gateway_ip": "192.168.100.1",
            }
        }

        # Mock extensions (tag support)
        client.list_extensions.return_value = {
            "extensions": [
                {"alias": "tag", "name": "Tag support"},
            ]
        }

        # Mock port creation
        client.create_port.return_value = {
            "port": {
                "id": "port-uuid-789",
                "name": "arca-svm-manila_project-123",
                "network_id": "net-uuid-123",
                "fixed_ips": [
                    {
                        "subnet_id": "subnet-uuid-456",
                        "ip_address": "192.168.100.10",
                    }
                ],
            }
        }

        # Mock port listing (no existing ports)
        client.list_ports.return_value = {"ports": []}

        return client

    @pytest.fixture
    def allocator(self, mock_config):
        """Create NeutronAllocator instance."""
        return NeutronAllocator(mock_config)

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_validate_config_success(
        self, mock_neutron_module, mock_ks_loading, allocator, mock_neutron_client
    ):
        """Test successful configuration validation."""
        # Mock auth and session loading
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client

        allocator.validate_config()

        # With new multi-network support, check _networks list instead of _vlan_id
        assert len(allocator._networks) == 1
        assert allocator._networks[0]["vlan_id"] == 100
        assert allocator._neutron_client is not None
        assert allocator._supports_tags is True

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    def test_validate_config_missing_net_id(self, mock_ks_loading, mock_config):
        """Test validation fails without network ID."""
        mock_config.arca_storage_neutron_net_ids = []  # Empty list
        allocator = NeutronAllocator(mock_config)

        with pytest.raises(ValueError, match="At least one network required"):
            allocator.validate_config()

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_validate_config_network_without_subnets(
        self, mock_neutron_module, mock_ks_loading, mock_config, mock_neutron_client
    ):
        """Test validation fails when network has no subnets."""
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client

        # Mock network without subnets
        mock_neutron_client.show_network.return_value = {
            "network": {
                "id": "net-uuid-123",
                "provider:network_type": "vlan",
                "provider:segmentation_id": 100,
                "subnets": [],  # No subnets
            }
        }

        allocator = NeutronAllocator(mock_config)

        with pytest.raises(ValueError, match="has no subnets"):
            allocator.validate_config()

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_validate_config_vxlan_network_rejected(
        self, mock_neutron_module, mock_ks_loading, allocator, mock_neutron_client
    ):
        """Test validation rejects VXLAN networks."""
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()

        # Mock VXLAN network
        mock_neutron_client.show_network.return_value = {
            "network": {
                "id": "net-uuid-123",
                "provider:network_type": "vxlan",
                "provider:segmentation_id": 5000,
                "subnets": ["subnet-uuid-456"],
            }
        }
        mock_neutron_module.Client.return_value = mock_neutron_client

        with pytest.raises(ValueError, match="must be a VLAN provider network"):
            allocator.validate_config()

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_validate_config_missing_segmentation_id(
        self, mock_neutron_module, mock_ks_loading, allocator, mock_neutron_client
    ):
        """Test validation fails without segmentation ID."""
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()

        # Mock network without segmentation_id
        mock_neutron_client.show_network.return_value = {
            "network": {
                "id": "net-uuid-123",
                "provider:network_type": "vlan",
                "subnets": ["subnet-uuid-456"],
            }
        }
        mock_neutron_module.Client.return_value = mock_neutron_client

        with pytest.raises(ValueError, match="missing provider:segmentation_id"):
            allocator.validate_config()

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_validate_config_missing_gateway(
        self, mock_neutron_module, mock_ks_loading, allocator, mock_neutron_client
    ):
        """Test validation fails without gateway IP."""
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()

        # Mock network (needed for first check)
        mock_neutron_client.show_network.return_value = {
            "network": {
                "id": "net-uuid-123",
                "provider:network_type": "vlan",
                "provider:segmentation_id": 100,
                "subnets": ["subnet-uuid-456"],
            }
        }

        # Mock subnet without gateway_ip
        mock_neutron_client.show_subnet.return_value = {
            "subnet": {
                "id": "subnet-uuid-456",
                "cidr": "192.168.100.0/24",
            }
        }
        mock_neutron_module.Client.return_value = mock_neutron_client

        with pytest.raises(ValueError, match="must have gateway_ip configured"):
            allocator.validate_config()

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_allocate_creates_port(
        self, mock_neutron_module, mock_ks_loading, allocator, mock_neutron_client
    ):
        """Test allocation creates Neutron port."""
        # Setup
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client
        allocator.validate_config()

        # Execute
        allocation = allocator.allocate("project-123", "manila_project-123")

        # Verify
        assert isinstance(allocation, NetworkAllocation)
        assert allocation.vlan_id == 100
        assert allocation.ip_cidr == "192.168.100.10/24"
        assert allocation.gateway == "192.168.100.1"
        assert allocation.allocation_id == "port-uuid-789"

        # Verify port creation call
        mock_neutron_client.create_port.assert_called_once()
        port_body = mock_neutron_client.create_port.call_args[0][0]
        assert port_body["port"]["device_owner"] == "compute:arca-storage-svm"
        assert port_body["port"]["device_id"] == "arca-svm-manila_project-123"

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_allocate_idempotent_reuses_port(
        self, mock_neutron_module, mock_ks_loading, allocator, mock_neutron_client
    ):
        """Test allocation is idempotent and reuses existing port."""
        # Setup
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client

        # Mock existing port
        existing_port = {
            "id": "existing-port-uuid",
            "network_id": "net-uuid-123",
            "fixed_ips": [
                {
                    "subnet_id": "subnet-uuid-456",
                    "ip_address": "192.168.100.15",
                }
            ],
        }
        mock_neutron_client.list_ports.return_value = {"ports": [existing_port]}

        allocator.validate_config()

        # Execute
        allocation = allocator.allocate("project-123", "manila_project-123")

        # Verify reused existing port
        assert allocation.allocation_id == "existing-port-uuid"
        assert allocation.ip_cidr == "192.168.100.15/24"

        # Should not create new port
        mock_neutron_client.create_port.assert_not_called()

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_allocate_handles_duplicate_ports(
        self, mock_neutron_module, mock_ks_loading, allocator, mock_neutron_client
    ):
        """Test allocation handles duplicate port race condition."""
        # Setup
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client

        # Mock duplicate ports (race condition)
        duplicate_ports = [
            {
                "id": "port-old",
                "network_id": "net-uuid-123",
                "created_at": "2025-01-01T00:00:00Z",
                "fixed_ips": [{"subnet_id": "subnet-uuid-456", "ip_address": "192.168.100.10"}],
            },
            {
                "id": "port-new",
                "network_id": "net-uuid-123",
                "created_at": "2025-01-01T00:01:00Z",
                "fixed_ips": [{"subnet_id": "subnet-uuid-456", "ip_address": "192.168.100.11"}],
            },
        ]

        # First call returns no ports, second call returns duplicates
        mock_neutron_client.list_ports.side_effect = [
            {"ports": []},  # _find_existing_port
            {"ports": duplicate_ports},  # After create_port
        ]

        allocator.validate_config()

        # Execute
        allocation = allocator.allocate("project-123", "manila_project-123")

        # Verify kept oldest port and deleted newer one
        assert allocation.allocation_id == "port-old"
        mock_neutron_client.delete_port.assert_called_once_with("port-new")

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_allocate_port_creation_failure(
        self, mock_neutron_module, mock_ks_loading, allocator, mock_neutron_client
    ):
        """Test allocation handles port creation failure."""
        # Setup
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client

        # Mock port creation failure
        mock_neutron_client.create_port.side_effect = Exception("Neutron API error")

        allocator.validate_config()

        # Execute and verify exception
        # Note: Generic Exception is wrapped as ArcaNetworkConflict with updated message
        with pytest.raises(arca_exceptions.ArcaNetworkConflict, match="Failed to create port"):
            allocator.allocate("project-123", "manila_project-123")

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_deallocate_deletes_port(
        self, mock_neutron_module, mock_ks_loading, allocator, mock_neutron_client
    ):
        """Test deallocation deletes Neutron port."""
        # Setup
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client
        allocator.validate_config()

        # Execute
        allocator.deallocate("port-uuid-789")

        # Verify
        mock_neutron_client.delete_port.assert_called_once_with("port-uuid-789")

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_deallocate_handles_missing_port(
        self, mock_neutron_module, mock_ks_loading, allocator, mock_neutron_client
    ):
        """Test deallocation handles missing port gracefully."""
        # Setup
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client

        # Mock port deletion failure
        mock_neutron_client.delete_port.side_effect = Exception("Port not found")

        allocator.validate_config()

        # Execute - should not raise exception
        allocator.deallocate("non-existent-port")

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_deallocate_with_none_allocation_id(
        self, mock_neutron_module, mock_ks_loading, allocator, mock_neutron_client
    ):
        """Test deallocation with None allocation_id is a no-op."""
        # Setup
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client
        allocator.validate_config()

        # Execute - should not raise exception
        allocator.deallocate(None)

        # Verify no port deletion attempted
        mock_neutron_client.delete_port.assert_not_called()

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_allocate_without_tag_extension(
        self, mock_neutron_module, mock_ks_loading, allocator, mock_neutron_client
    ):
        """Test allocation works without tag extension."""
        # Setup
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client

        # Mock no tag extension
        mock_neutron_client.list_extensions.return_value = {"extensions": []}

        allocator.validate_config()

        # Verify tag support disabled
        assert allocator._supports_tags is False

        # Execute - should still work
        allocation = allocator.allocate("project-123", "manila_project-123")
        assert allocation is not None

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_allocate_with_multiple_fixed_ips(
        self, mock_neutron_module, mock_ks_loading, allocator, mock_neutron_client
    ):
        """Test allocation uses first fixed IP when port has multiple."""
        # Setup
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client

        # Mock port with multiple fixed IPs
        mock_neutron_client.create_port.return_value = {
            "port": {
                "id": "port-uuid-789",
                "network_id": "net-uuid-123",
                "fixed_ips": [
                    {"subnet_id": "subnet-uuid-456", "ip_address": "192.168.100.10"},
                    {"subnet_id": "subnet-uuid-789", "ip_address": "192.168.101.10"},
                ],
            }
        }

        allocator.validate_config()

        # Execute
        allocation = allocator.allocate("project-123", "manila_project-123")

        # Verify uses first fixed IP
        assert allocation.ip_cidr == "192.168.100.10/24"

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    def test_create_neutron_client_no_auth(self, mock_ks_loading, allocator):
        """Test Neutron client creation fails without auth config."""
        # Mock no auth configured
        mock_ks_loading.load_auth_from_conf_options.return_value = None

        with pytest.raises(ValueError, match="Neutron authentication not configured"):
            allocator.validate_config()


class TestNeutronAllocatorMultipleNetworks:
    """Tests for NeutronAllocator with multiple networks."""

    @pytest.fixture
    def mock_config_multi_net(self):
        """Create mock configuration with multiple networks."""
        config = Mock()
        config.arca_storage_neutron_net_ids = [
            "net-uuid-100",
            "net-uuid-200",
            "net-uuid-300",
        ]
        config.arca_storage_neutron_port_security = False
        config.arca_storage_neutron_vnic_type = "normal"
        # Old options should be None
        config.arca_storage_neutron_net_id = None
        config.arca_storage_neutron_subnet_id = None
        return config

    @pytest.fixture
    def mock_neutron_client_multi(self):
        """Create mock Neutron client for multiple networks."""
        client = Mock()

        def show_network_side_effect(net_id):
            """Return different network data based on net_id."""
            networks = {
                "net-uuid-100": {
                    "network": {
                        "id": "net-uuid-100",
                        "name": "vlan-100-net",
                        "provider:network_type": "vlan",
                        "provider:segmentation_id": 100,
                        "subnets": ["subnet-uuid-100"],
                    }
                },
                "net-uuid-200": {
                    "network": {
                        "id": "net-uuid-200",
                        "name": "vlan-200-net",
                        "provider:network_type": "vlan",
                        "provider:segmentation_id": 200,
                        "subnets": ["subnet-uuid-200"],
                    }
                },
                "net-uuid-300": {
                    "network": {
                        "id": "net-uuid-300",
                        "name": "vlan-300-net",
                        "provider:network_type": "vlan",
                        "provider:segmentation_id": 300,
                        "subnets": ["subnet-uuid-300"],
                    }
                },
            }
            return networks.get(net_id, {"network": {}})

        def show_subnet_side_effect(subnet_id):
            """Return different subnet data based on subnet_id."""
            subnets = {
                "subnet-uuid-100": {
                    "subnet": {
                        "id": "subnet-uuid-100",
                        "cidr": "192.168.100.0/24",
                        "gateway_ip": "192.168.100.1",
                    }
                },
                "subnet-uuid-200": {
                    "subnet": {
                        "id": "subnet-uuid-200",
                        "cidr": "192.168.200.0/24",
                        "gateway_ip": "192.168.200.1",
                    }
                },
                "subnet-uuid-300": {
                    "subnet": {
                        "id": "subnet-uuid-300",
                        "cidr": "10.0.0.0/24",
                        "gateway_ip": "10.0.0.1",
                    }
                },
            }
            return subnets.get(subnet_id, {"subnet": {}})

        client.show_network.side_effect = show_network_side_effect
        client.show_subnet.side_effect = show_subnet_side_effect

        # Mock extensions (tag support)
        client.list_extensions.return_value = {
            "extensions": [{"alias": "tag", "name": "Tag support"}]
        }

        # Mock port listing (no existing ports)
        client.list_ports.return_value = {"ports": []}

        return client

    @pytest.fixture
    def allocator_multi(self, mock_config_multi_net):
        """Create NeutronAllocator instance with multiple networks."""
        return NeutronAllocator(mock_config_multi_net)

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_validate_config_multiple_networks(
        self, mock_neutron_module, mock_ks_loading, allocator_multi, mock_neutron_client_multi
    ):
        """Test configuration validation with multiple networks."""
        # Mock auth and session loading
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client_multi

        allocator_multi.validate_config()

        # Verify all 3 networks were cached
        assert len(allocator_multi._networks) == 3
        assert allocator_multi._networks[0]["vlan_id"] == 100
        assert allocator_multi._networks[1]["vlan_id"] == 200
        assert allocator_multi._networks[2]["vlan_id"] == 300

        # Verify subnets were auto-detected
        assert allocator_multi._networks[0]["subnet_id"] == "subnet-uuid-100"
        assert allocator_multi._networks[1]["subnet_id"] == "subnet-uuid-200"
        assert allocator_multi._networks[2]["subnet_id"] == "subnet-uuid-300"

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_allocate_round_robin_selection(
        self, mock_neutron_module, mock_ks_loading, allocator_multi, mock_neutron_client_multi
    ):
        """Test round-robin network selection across multiple allocations."""
        # Setup
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client_multi

        # Track created ports with their device_id
        created_ports = []

        def create_port_side_effect(port_body):
            port_network_id = port_body["port"]["network_id"]
            port_subnet_id = port_body["port"]["fixed_ips"][0]["subnet_id"]
            device_id = port_body["port"]["device_id"]

            # Determine IP based on network
            subnet_num = port_subnet_id.split("-")[-1]
            # Use 10.0.0.x for subnet-uuid-300, 192.168.x.x for others
            if subnet_num == "300":
                ip_address = "10.0.0.10"
            else:
                ip_address = f"192.168.{subnet_num}.10"

            port = {
                "port": {
                    "id": f"port-{len(created_ports)}",
                    "network_id": port_network_id,
                    "device_id": device_id,
                    "fixed_ips": [
                        {"subnet_id": port_subnet_id, "ip_address": ip_address}
                    ],
                }
            }
            created_ports.append(port["port"])
            return port

        mock_neutron_client_multi.create_port.side_effect = create_port_side_effect

        # Mock list_ports to return ports matching device_id
        def list_ports_side_effect(**kwargs):
            device_id = kwargs.get("device_id")
            device_owner = kwargs.get("device_owner")

            if device_id:
                # Return ports matching this specific device_id
                matching = [p for p in created_ports if p.get("device_id") == device_id]
                return {"ports": matching}
            elif device_owner:
                # Return all ports with this device_owner
                return {"ports": created_ports}

            return {"ports": []}

        mock_neutron_client_multi.list_ports.side_effect = list_ports_side_effect

        allocator_multi.validate_config()

        # Allocate 3 times - should use network 100, 200, 300 in order
        alloc1 = allocator_multi.allocate("project-1", "manila_project-1")
        assert alloc1.vlan_id == 100  # First network
        assert alloc1.ip_cidr.startswith("192.168.100.")

        alloc2 = allocator_multi.allocate("project-2", "manila_project-2")
        assert alloc2.vlan_id == 200  # Second network
        assert alloc2.ip_cidr.startswith("192.168.200.")

        alloc3 = allocator_multi.allocate("project-3", "manila_project-3")
        assert alloc3.vlan_id == 300  # Third network
        assert alloc3.ip_cidr.startswith("10.0.0.")

        # Fourth allocation wraps back to network 100
        alloc4 = allocator_multi.allocate("project-4", "manila_project-4")
        assert alloc4.vlan_id == 100
        assert alloc4.ip_cidr.startswith("192.168.100.")

    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_extract_allocation_from_existing_port_multi_network(
        self, mock_neutron_module, mock_ks_loading, allocator_multi, mock_neutron_client_multi
    ):
        """Test extracting allocation from existing port in multi-network setup."""
        # Setup
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()
        mock_neutron_module.Client.return_value = mock_neutron_client_multi

        allocator_multi.validate_config()

        # Mock existing port on network 200
        existing_port = {
            "id": "port-existing",
            "network_id": "net-uuid-200",
            "fixed_ips": [
                {"subnet_id": "subnet-uuid-200", "ip_address": "192.168.200.50"}
            ],
        }

        mock_neutron_client_multi.list_ports.return_value = {"ports": [existing_port]}

        # Allocate - should reuse existing port
        allocation = allocator_multi.allocate("project-123", "manila_project-123")

        # Verify allocation uses network 200 metadata from cache
        assert allocation.vlan_id == 200
        assert allocation.ip_cidr == "192.168.200.50/24"
        assert allocation.gateway == "192.168.200.1"
        assert allocation.allocation_id == "port-existing"
