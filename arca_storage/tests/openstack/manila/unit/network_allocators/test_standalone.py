"""Unit tests for StandaloneAllocator."""

import threading
from unittest.mock import Mock, patch

import pytest

from arca_storage.openstack.manila import exceptions as arca_exceptions
from arca_storage.openstack.manila.network_allocators import standalone as standalone_allocator
from arca_storage.openstack.manila.network_allocators.standalone import (
    StandaloneAllocator,
)
from arca_storage.openstack.manila.network_allocators.base import NetworkAllocation


def render_log_calls(*mock_logs):
    rendered = []
    for mock_log in mock_logs:
        rendered.extend(str(call.args) for call in mock_log.call_args_list)
    return " ".join(rendered)


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
        with patch.object(standalone_allocator.LOG, "debug") as mock_debug:
            allocator.validate_config()
        assert len(allocator._ip_vlan_pools) == 2
        assert allocator._ip_vlan_pools[0]["vlan_id"] == 100
        assert allocator._ip_vlan_pools[1]["vlan_id"] == 101
        rendered_calls = render_log_calls(mock_debug)
        assert "192.168.100.0" not in rendered_calls
        assert "192.168.101.0" not in rendered_calls
        assert "192.168.100.10" not in rendered_calls
        assert "192.168.101.20" not in rendered_calls
        assert "Parsed standalone IP pool %d with %d IPs" in rendered_calls

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

        with patch.object(standalone_allocator.LOG, "debug") as mock_debug:
            allocation = allocator.allocate("project-123", "manila_project-123")

        assert isinstance(allocation, NetworkAllocation)
        assert allocation.vlan_id == 100
        assert allocation.ip_cidr.startswith("192.168.100.")
        assert allocation.gateway == "192.168.100.1"
        assert allocation.allocation_id is None
        rendered_calls = render_log_calls(mock_debug)
        assert "project-123" not in rendered_calls
        assert "manila_project-123" not in rendered_calls
        assert "192.168.100." not in rendered_calls
        assert "Allocated standalone network from pool %d" in rendered_calls
        assert "Found free IP in standalone pool" in rendered_calls

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

    def test_allocate_redacts_sensitive_unexpected_errors(self, allocator):
        """Unexpected allocation errors should not leak credentials."""
        allocator.validate_config()
        allocator._allocate_from_multi_pool = Mock(
            side_effect=Exception("Authorization: Bearer secret-token password=hunter2")
        )

        with patch.object(standalone_allocator.LOG, "error") as mock_error:
            with pytest.raises(arca_exceptions.ArcaNetworkConflict) as exc_info:
                allocator.allocate("project-123", "manila_project-123")

        assert "secret-token" not in str(exc_info.value)
        assert "hunter2" not in str(exc_info.value)
        assert "<redacted>" in str(exc_info.value)
        rendered_calls = render_log_calls(mock_error)
        assert "project-123" not in rendered_calls
        assert "manila_project-123" not in rendered_calls
        assert "secret-token" not in rendered_calls
        assert "hunter2" not in rendered_calls
        assert "Failed to allocate standalone network" in rendered_calls

    def test_allocate_redacts_sensitive_pool_error_logs(self, allocator):
        """Unexpected pool errors should not leak details into logs."""
        allocator.validate_config()
        allocator._find_free_slot_in_pool = Mock(
            side_effect=RuntimeError(
                "Authorization: Bearer secret-token password=hunter2"
            )
        )

        with patch.object(standalone_allocator.LOG, "warning") as mock_warning:
            with pytest.raises(arca_exceptions.ArcaNetworkPoolExhausted):
                allocator.allocate("project-123", "manila_project-123")

        rendered_calls = render_log_calls(mock_warning)
        assert "project-123" not in rendered_calls
        assert "manila_project-123" not in rendered_calls
        assert "secret-token" not in rendered_calls
        assert "hunter2" not in rendered_calls
        assert (
            "Standalone network pool allocation failed with unexpected error"
            in rendered_calls
        )

    def test_get_used_ips_logs_redact_malformed_svm_metadata(
        self, allocator, mock_arca_client
    ):
        """Malformed SVM metadata logs should not expose SVM names or IP values."""
        allocator.validate_config()
        mock_arca_client.list_svms.return_value = [
            {
                "name": "svm-secret-token",
                "vlan_id": "bad-secret-token",
                "vip": "192.168.100.10",
            },
            {
                "name": "svm-secret-token",
                "vlan_id": 100,
                "vip": "not-an-ip-secret-token",
            },
            {
                "name": "svm-secret-token",
                "vlan_id": 100,
                "ip_cidr": "not-a-cidr-secret-token",
            },
        ]

        with patch.object(standalone_allocator.LOG, "warning") as mock_warning:
            allocator._get_used_ips_in_vlan(100)

        rendered_calls = render_log_calls(mock_warning)
        assert "svm-secret-token" not in rendered_calls
        assert "bad-secret-token" not in rendered_calls
        assert "not-an-ip-secret-token" not in rendered_calls
        assert "not-a-cidr-secret-token" not in rendered_calls
        assert "192.168.100.10" not in rendered_calls
        assert "Skipping SVM with invalid VLAN metadata" in rendered_calls
        assert "Invalid VIP format in SVM metadata" in rendered_calls
        assert "Invalid ip_cidr format in SVM metadata" in rendered_calls

    def test_get_used_ips_logs_redact_list_errors(self, allocator, mock_arca_client):
        """SVM list failures should not leak backend details into logs."""
        allocator.validate_config()
        mock_arca_client.list_svms.side_effect = RuntimeError(
            "Authorization: Bearer secret-token password=hunter2"
        )

        with patch.object(standalone_allocator.LOG, "warning") as mock_warning:
            used_ips = allocator._get_used_ips_in_vlan(100)

        assert used_ips == set()
        rendered_calls = render_log_calls(mock_warning)
        assert "100" not in rendered_calls
        assert "secret-token" not in rendered_calls
        assert "hunter2" not in rendered_calls
        assert "Failed to get used IPs for standalone allocation" in rendered_calls

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
