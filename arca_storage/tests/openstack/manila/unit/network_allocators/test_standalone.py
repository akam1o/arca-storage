"""Unit tests for StandaloneAllocator."""

import ipaddress
import threading
from unittest.mock import Mock, patch

import pytest

from arca_storage.openstack.manila import exceptions as arca_exceptions
from arca_storage.openstack.manila.network_allocators.standalone import (
    StandaloneAllocator,
)
from arca_storage.openstack.manila.network_allocators.base import NetworkAllocation


class TestStandaloneAllocator:
    """Tests for StandaloneAllocator."""

    @pytest.fixture
    def mock_config(self):
        """Create mock configuration."""
        config = Mock()
        config.arca_storage_per_project_ip_pools = [
            "192.168.100.0/24|192.168.100.10-192.168.100.20:100",
            "192.168.101.0/24|192.168.101.10-192.168.101.20:101",
        ]
        config.arca_storage_per_project_mtu = 1500
        config.arca_storage_per_project_root_volume_size_gib = 10
        return config

    @pytest.fixture
    def mock_arca_client(self):
        """Create mock ARCA client."""
        client = Mock()
        client.list_svms.return_value = []
        return client

    @pytest.fixture
    def allocator(self, mock_config, mock_arca_client):
        """Create StandaloneAllocator instance."""
        allocation_lock = threading.Lock()
        pool_counter = 0
        allocator = StandaloneAllocator(
            mock_config, mock_arca_client, allocation_lock, pool_counter
        )
        return allocator

    def test_validate_config_success(self, allocator):
        """Test successful configuration validation."""
        allocator.validate_config()
        assert len(allocator._ip_vlan_pools) == 2
        assert allocator._ip_vlan_pools[0]["vlan_id"] == 100
        assert allocator._ip_vlan_pools[1]["vlan_id"] == 101

    def test_validate_config_no_pools(self, mock_arca_client):
        """Test validation fails with no pools configured."""
        config = Mock()
        config.arca_storage_per_project_ip_pools = []
        allocator = StandaloneAllocator(
            config, mock_arca_client, threading.Lock(), 0
        )

        with pytest.raises(arca_exceptions.ArcaNetworkConfigurationError, match="arca_storage_per_project_ip_pools is required"):
            allocator.validate_config()

    def test_validate_config_invalid_format(self, mock_arca_client):
        """Test validation fails with invalid pool format."""
        config = Mock()
        config.arca_storage_per_project_ip_pools = ["invalid-format"]
        allocator = StandaloneAllocator(
            config, mock_arca_client, threading.Lock(), 0
        )

        with pytest.raises(arca_exceptions.ArcaNetworkConfigurationError, match="Invalid pool configuration"):
            allocator.validate_config()

    def test_validate_config_invalid_vlan_range(self, mock_arca_client):
        """Test validation fails with invalid VLAN ID."""
        config = Mock()
        config.arca_storage_per_project_ip_pools = [
            "192.168.100.0/24|192.168.100.10-192.168.100.20:5000"  # VLAN > 4094
        ]
        allocator = StandaloneAllocator(
            config, mock_arca_client, threading.Lock(), 0
        )

        with pytest.raises(arca_exceptions.ArcaNetworkConfigurationError, match="VLAN ID .* out of range"):
            allocator.validate_config()

    def test_validate_config_gateway_in_range(self, mock_arca_client):
        """Test validation fails when gateway is in allocatable range."""
        config = Mock()
        # Gateway 192.168.100.1 is in range 192.168.100.1-192.168.100.20
        config.arca_storage_per_project_ip_pools = [
            "192.168.100.0/24|192.168.100.1-192.168.100.20:100"
        ]
        allocator = StandaloneAllocator(
            config, mock_arca_client, threading.Lock(), 0
        )

        with pytest.raises(arca_exceptions.ArcaNetworkConfigurationError, match="Gateway IP .* is within allocatable range"):
            allocator.validate_config()

    def test_allocate_success(self, allocator, mock_arca_client):
        """Test successful network allocation."""
        allocator.validate_config()
        mock_arca_client.list_svms.return_value = []

        allocation = allocator.allocate("project-123", "manila_project-123")

        assert isinstance(allocation, NetworkAllocation)
        assert allocation.vlan_id == 100
        assert allocation.ip_cidr.startswith("192.168.100.")
        assert allocation.gateway == "192.168.100.1"
        assert allocation.allocation_id is None

    def test_allocate_round_robin(self, allocator, mock_arca_client):
        """Test round-robin pool selection."""
        allocator.validate_config()
        mock_arca_client.list_svms.return_value = []

        # First allocation from pool 0
        alloc1 = allocator.allocate("project-1", "manila_project-1")
        assert alloc1.vlan_id == 100

        # Second allocation from pool 1 (round-robin)
        alloc2 = allocator.allocate("project-2", "manila_project-2")
        assert alloc2.vlan_id == 101

        # Third allocation wraps back to pool 0
        alloc3 = allocator.allocate("project-3", "manila_project-3")
        assert alloc3.vlan_id == 100

    def test_allocate_with_ip_conflict(self, allocator, mock_arca_client):
        """Test allocation skips already used IPs."""
        allocator.validate_config()

        # Mock existing SVM using 192.168.100.10
        mock_arca_client.list_svms.return_value = [
            {
                "name": "existing-svm",
                "vlan_id": 100,
                "vip": "192.168.100.10",
            }
        ]

        allocation = allocator.allocate("project-123", "manila_project-123")

        # Should allocate next available IP (192.168.100.11)
        assert allocation.ip_cidr == "192.168.100.11/24"

    def test_allocate_pool_exhausted(self, allocator, mock_arca_client):
        """Test allocation fails when all pools are exhausted."""
        allocator.validate_config()

        # Mock all IPs in both pools as used
        used_svms = []
        for vlan, start, end in [(100, 10, 20), (101, 10, 20)]:
            for i in range(start, end + 1):
                used_svms.append({
                    "name": f"svm-{vlan}-{i}",
                    "vlan_id": vlan,
                    "vip": f"192.168.{vlan}.{i}",
                })

        mock_arca_client.list_svms.return_value = used_svms

        with pytest.raises(arca_exceptions.ArcaNetworkPoolExhausted, match="All .* IP/VLAN pools exhausted"):
            allocator.allocate("project-123", "manila_project-123")

    def test_allocate_with_retry(self, allocator, mock_arca_client):
        """Test allocation with retry attempt uses random offset."""
        allocator.validate_config()
        mock_arca_client.list_svms.return_value = []

        # Retry attempt should use randomized offset
        allocation = allocator.allocate("project-123", "manila_project-123", retry_attempt=1)

        assert isinstance(allocation, NetworkAllocation)
        assert allocation.vlan_id in [100, 101]

    def test_deallocate_is_noop(self, allocator):
        """Test deallocate is a no-op for standalone mode."""
        allocator.validate_config()
        # Should not raise any exception
        allocator.deallocate("any-allocation-id")

    def test_parse_single_ip_pool(self, mock_arca_client):
        """Test parsing pool with single IP (start == end)."""
        config = Mock()
        config.arca_storage_per_project_ip_pools = [
            "192.168.100.0/24|192.168.100.10-192.168.100.10:100"
        ]
        allocator = StandaloneAllocator(
            config, mock_arca_client, threading.Lock(), 0
        )

        allocator.validate_config()
        assert allocator._ip_vlan_pools[0]["num_hosts"] == 1

    def test_parse_ipv6_pool_rejected(self, mock_arca_client):
        """Test IPv6 pools are rejected (currently due to invalid format)."""
        config = Mock()
        config.arca_storage_per_project_ip_pools = [
            "2001:db8::/64|2001:db8::10-2001:db8::20:100"
        ]
        allocator = StandaloneAllocator(
            config, mock_arca_client, threading.Lock(), 0
        )

        # IPv6 parsing currently fails due to colons in the format
        # This test verifies the error is raised, though the message differs
        with pytest.raises(arca_exceptions.ArcaNetworkConfigurationError, match="Invalid"):
            allocator.validate_config()

    def test_allocate_handles_vlan_string(self, allocator, mock_arca_client):
        """Test allocation handles VLAN ID as string from API."""
        allocator.validate_config()

        # Mock SVM with VLAN as string (API may return string)
        mock_arca_client.list_svms.return_value = [
            {
                "name": "existing-svm",
                "vlan_id": "100",  # String instead of int
                "vip": "192.168.100.10",
            }
        ]

        allocation = allocator.allocate("project-123", "manila_project-123")

        # Should handle string VLAN and skip the used IP
        assert allocation.ip_cidr != "192.168.100.10/24"

    def test_allocate_handles_ip_cidr_format(self, allocator, mock_arca_client):
        """Test allocation handles both VIP and ip_cidr formats."""
        allocator.validate_config()

        # Mock SVMs with different IP formats
        mock_arca_client.list_svms.return_value = [
            {
                "name": "svm1",
                "vlan_id": 100,
                "vip": "192.168.100.10/24",  # CIDR format
            },
            {
                "name": "svm2",
                "vlan_id": 100,
                "ip_cidr": "192.168.100.11/24",  # ip_cidr field
            },
        ]

        allocation = allocator.allocate("project-123", "manila_project-123")

        # Should skip both 192.168.100.10 and 192.168.100.11
        assert allocation.ip_cidr not in ["192.168.100.10/24", "192.168.100.11/24"]
