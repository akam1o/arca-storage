"""Integration tests for driver with NetworkAllocator modes."""

from unittest.mock import Mock, patch, MagicMock

import pytest
from oslo_config import cfg

from arca_storage.openstack.manila import exceptions as arca_exceptions
from arca_storage.openstack.manila.driver import ArcaStorageManilaDriver

CONF = cfg.CONF


class TestDriverNetworkAllocators:
    """Tests for driver NetworkAllocator integration."""

    @pytest.fixture(autouse=True)
    def setup_oslo_config(self):
        """Setup oslo.config for tests."""
        # Set lock_path for oslo_concurrency
        CONF.set_override('lock_path', '/tmp', group='oslo_concurrency')
        yield
        # Cleanup
        CONF.clear_override('lock_path', group='oslo_concurrency')

    @pytest.fixture
    def mock_arca_client(self):
        """Create mock ARCA client."""
        client = Mock()
        client.list_svms.return_value = []
        return client

    @pytest.fixture
    def base_config(self):
        """Create base configuration."""
        config = Mock()
        config.arca_storage_use_api = True
        config.arca_storage_api_endpoint = "http://localhost:8080"
        config.arca_storage_api_timeout = 30
        config.arca_storage_api_retry_count = 3
        config.arca_storage_verify_ssl = True
        config.arca_storage_api_auth_type = "none"
        config.arca_storage_api_token = None
        config.arca_storage_api_username = None
        config.arca_storage_api_password = None
        config.arca_storage_api_ca_bundle = None
        config.arca_storage_api_client_cert = None
        config.arca_storage_api_client_key = None
        config.arca_storage_svm_prefix = "manila_"
        config.arca_storage_per_project_mtu = 1500
        config.arca_storage_per_project_root_volume_size_gib = 10
        config.oslo_concurrency_lock_path = "/tmp"  # Required for distributed locking
        return config

    @patch("arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient")
    def test_driver_init_standalone_mode(self, mock_client_class, base_config, mock_arca_client):
        """Test driver initialization with standalone mode."""
        # Setup
        base_config.arca_storage_svm_strategy = "per_project"
        base_config.arca_storage_network_plugin_mode = "standalone"
        base_config.arca_storage_per_project_ip_pools = [
            "192.168.100.0/24|192.168.100.10-192.168.100.20:100"
        ]
        mock_client_class.return_value = mock_arca_client

        # Create driver
        driver = ArcaStorageManilaDriver(configuration=base_config)
        driver.do_setup(Mock())

        # Verify standalone allocator initialized
        assert driver._network_allocator is not None
        assert driver._network_allocator.__class__.__name__ == "StandaloneAllocator"

    @patch("arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_driver_init_neutron_mode(
        self, mock_neutron_module, mock_ks_loading, mock_client_class, base_config, mock_arca_client
    ):
        """Test driver initialization with Neutron mode."""
        # Setup
        base_config.arca_storage_svm_strategy = "per_project"
        base_config.arca_storage_network_plugin_mode = "neutron"
        # Removed deprecated option
        # Removed deprecated option
        base_config.arca_storage_neutron_net_ids = ["net-uuid-123"]
        base_config.arca_storage_neutron_port_security = False
        base_config.arca_storage_neutron_vnic_type = "normal"

        mock_client_class.return_value = mock_arca_client

        # Mock Neutron client initialization
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()

        mock_neutron_client = Mock()
        mock_neutron_client.show_network.return_value = {
            "network": {
                "id": "net-uuid-123",
                "provider:network_type": "vlan",
                "provider:segmentation_id": 100,
                "subnets": ["subnet-uuid-456"],
            }
        }
        mock_neutron_client.show_subnet.return_value = {
            "subnet": {
                "id": "subnet-uuid-456",
                "cidr": "192.168.100.0/24",
                "gateway_ip": "192.168.100.1",
                "ip_version": 4,
            }
        }
        mock_neutron_client.list_extensions.return_value = {"extensions": []}
        mock_neutron_module.Client.return_value = mock_neutron_client

        # Create driver
        driver = ArcaStorageManilaDriver(configuration=base_config)
        driver.do_setup(Mock())

        # Verify Neutron allocator initialized
        assert driver._network_allocator is not None
        assert driver._network_allocator.__class__.__name__ == "NeutronAllocator"

    @patch("arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient")
    def test_driver_init_invalid_network_mode(self, mock_client_class, base_config, mock_arca_client):
        """Test driver initialization fails with invalid network mode."""
        # Setup
        base_config.arca_storage_svm_strategy = "per_project"
        base_config.arca_storage_network_plugin_mode = "invalid-mode"
        mock_client_class.return_value = mock_arca_client

        # Create driver
        driver = ArcaStorageManilaDriver(configuration=base_config)

        # Verify initialization fails
        with pytest.raises(Exception, match="Invalid network_plugin_mode"):
            driver.do_setup(Mock())

    @patch("arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient")
    def test_driver_per_project_svm_allocation_standalone(
        self, mock_client_class, base_config, mock_arca_client
    ):
        """Test per-project SVM allocation with standalone mode."""
        # Setup
        base_config.arca_storage_svm_strategy = "per_project"
        base_config.arca_storage_network_plugin_mode = "standalone"
        base_config.arca_storage_per_project_ip_pools = [
            "192.168.100.0/24|192.168.100.10-192.168.100.20:100"
        ]
        mock_client_class.return_value = mock_arca_client

        # Mock SVM not found, then created
        mock_arca_client.get_svm.side_effect = arca_exceptions.ArcaSVMNotFound(svm_name="test")
        mock_arca_client.create_svm.return_value = {
            "name": "manila_project-123",
            "vip": "192.168.100.10",
        }

        # Create driver
        driver = ArcaStorageManilaDriver(configuration=base_config)
        driver.do_setup(Mock())

        # Allocate SVM
        svm_name = driver._allocate_per_project_svm("project-123")

        # Verify SVM created with network from allocator
        assert svm_name == "manila_project-123"
        mock_arca_client.create_svm.assert_called_once()
        call_args = mock_arca_client.create_svm.call_args[1]
        assert call_args["vlan_id"] == 100
        assert call_args["ip_cidr"].startswith("192.168.100.")
        assert call_args["gateway"] == "192.168.100.1"

    @patch("arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_driver_per_project_svm_allocation_neutron(
        self, mock_neutron_module, mock_ks_loading, mock_client_class, base_config, mock_arca_client
    ):
        """Test per-project SVM allocation with Neutron mode."""
        # Setup
        base_config.arca_storage_svm_strategy = "per_project"
        base_config.arca_storage_network_plugin_mode = "neutron"
        # Removed deprecated option
        # Removed deprecated option
        base_config.arca_storage_neutron_net_ids = ["net-uuid-123"]
        base_config.arca_storage_neutron_port_security = False
        base_config.arca_storage_neutron_vnic_type = "normal"

        mock_client_class.return_value = mock_arca_client

        # Mock Neutron client
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()

        mock_neutron_client = Mock()
        mock_neutron_client.show_network.return_value = {
            "network": {
                "id": "net-uuid-123",
                "provider:network_type": "vlan",
                "provider:segmentation_id": 100,
                "subnets": ["subnet-uuid-456"],
            }
        }
        mock_neutron_client.show_subnet.return_value = {
            "subnet": {
                "id": "subnet-uuid-456",
                "cidr": "192.168.100.0/24",
                "gateway_ip": "192.168.100.1",
                "ip_version": 4,
            }
        }
        mock_neutron_client.list_extensions.return_value = {"extensions": []}
        mock_neutron_client.list_ports.return_value = {"ports": []}
        mock_neutron_client.create_port.return_value = {
            "port": {
                "id": "port-uuid-789",
                "network_id": "net-uuid-123",
                "fixed_ips": [
                    {"subnet_id": "subnet-uuid-456", "ip_address": "192.168.100.10"}
                ],
            }
        }
        mock_neutron_module.Client.return_value = mock_neutron_client

        # Mock SVM not found, then created
        mock_arca_client.get_svm.side_effect = arca_exceptions.ArcaSVMNotFound(svm_name="test")
        mock_arca_client.create_svm.return_value = {
            "name": "manila_project-123",
            "vip": "192.168.100.10",
        }

        # Create driver
        driver = ArcaStorageManilaDriver(configuration=base_config)
        driver.do_setup(Mock())

        # Allocate SVM
        svm_name = driver._allocate_per_project_svm("project-123")

        # Verify SVM created with network from Neutron
        assert svm_name == "manila_project-123"
        mock_arca_client.create_svm.assert_called_once()
        call_args = mock_arca_client.create_svm.call_args[1]
        assert call_args["vlan_id"] == 100
        assert call_args["ip_cidr"] == "192.168.100.10/24"
        assert call_args["gateway"] == "192.168.100.1"

        # Verify Neutron port created
        mock_neutron_client.create_port.assert_called_once()

    @patch("arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.ks_loading")
    @patch("arca_storage.openstack.manila.network_allocators.neutron.neutron_client")
    def test_driver_network_conflict_cleanup(
        self, mock_neutron_module, mock_ks_loading, mock_client_class, base_config, mock_arca_client
    ):
        """Test driver cleans up allocated port on SVM creation failure."""
        # Setup
        base_config.arca_storage_svm_strategy = "per_project"
        base_config.arca_storage_network_plugin_mode = "neutron"
        # Removed deprecated option
        # Removed deprecated option
        base_config.arca_storage_neutron_net_ids = ["net-uuid-123"]
        base_config.arca_storage_neutron_port_security = False
        base_config.arca_storage_neutron_vnic_type = "normal"

        mock_client_class.return_value = mock_arca_client

        # Mock Neutron client
        mock_ks_loading.load_auth_from_conf_options.return_value = Mock()
        mock_ks_loading.load_session_from_conf_options.return_value = Mock()

        mock_neutron_client = Mock()
        mock_neutron_client.show_network.return_value = {
            "network": {
                "id": "net-uuid-123",
                "provider:network_type": "vlan",
                "provider:segmentation_id": 100,
                "subnets": ["subnet-uuid-456"],
            }
        }
        mock_neutron_client.show_subnet.return_value = {
            "subnet": {
                "id": "subnet-uuid-456",
                "cidr": "192.168.100.0/24",
                "gateway_ip": "192.168.100.1",
                "ip_version": 4,
            }
        }
        mock_neutron_client.list_extensions.return_value = {"extensions": []}
        mock_neutron_client.list_ports.return_value = {"ports": []}
        mock_neutron_client.create_port.return_value = {
            "port": {
                "id": "port-uuid-789",
                "network_id": "net-uuid-123",
                "fixed_ips": [
                    {"subnet_id": "subnet-uuid-456", "ip_address": "192.168.100.10"}
                ],
            }
        }
        mock_neutron_module.Client.return_value = mock_neutron_client

        # Mock SVM not found, then creation fails
        mock_arca_client.get_svm.side_effect = arca_exceptions.ArcaSVMNotFound(svm_name="test")
        mock_arca_client.create_svm.side_effect = Exception("Backend error")

        # Create driver
        driver = ArcaStorageManilaDriver(configuration=base_config)
        driver.do_setup(Mock())

        # Attempt to allocate SVM (should fail)
        with pytest.raises(Exception, match="Failed to create SVM"):
            driver._allocate_per_project_svm("project-123")

        # Verify port was cleaned up
        mock_neutron_client.delete_port.assert_called_once_with("port-uuid-789")

    @patch("arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient")
    def test_driver_shared_strategy_no_allocator(self, mock_client_class, base_config, mock_arca_client):
        """Test driver with shared strategy does not initialize allocator."""
        # Setup
        base_config.arca_storage_svm_strategy = "shared"
        base_config.arca_storage_default_svm = "default-svm"
        mock_client_class.return_value = mock_arca_client

        # Mock default SVM exists
        mock_arca_client.get_svm.return_value = {
            "name": "default-svm",
            "vip": "192.168.100.5",
        }

        # Create driver
        driver = ArcaStorageManilaDriver(configuration=base_config)
        driver.do_setup(Mock())

        # Verify no allocator initialized (shared strategy doesn't need it)
        assert not hasattr(driver, "_network_allocator") or driver._network_allocator is None
