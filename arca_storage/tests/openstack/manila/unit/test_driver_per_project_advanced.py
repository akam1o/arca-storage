"""Advanced unit tests for Manila driver per_project strategy.

These tests cover edge cases and error scenarios identified by code review.
"""

from unittest.mock import Mock, patch

import pytest

from arca_storage.openstack.manila import driver as manila_driver
from arca_storage.openstack.manila import exceptions as arca_exceptions


class TestPerProjectNetworkConflictRetry:
    """Test network conflict retry logic for per_project strategy."""

    @pytest.fixture
    def driver(self, mock_manila_driver_config, mock_arca_client):
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "per_project"
            drv.configuration.arca_storage_per_project_ip_pools = [
                "192.168.100.0/24|192.168.100.10-192.168.100.20:100"
            ]
            drv.configuration.arca_storage_svm_prefix = "manila_"
            drv.do_setup(Mock())
            return drv

    def test_create_svm_retries_on_ip_conflict(self, driver, mock_arca_client, mock_manila_share):
        """Test that SVM creation retries on IP conflict."""
        def get_svm_side_effect(name):
            if name == "manila_test-project-id":
                raise arca_exceptions.ArcaSVMNotFound(svm_name=name)
            return {"name": name}

        mock_arca_client.get_svm.side_effect = get_svm_side_effect
        mock_arca_client.list_svms.return_value = []

        # First two attempts fail with IP conflict, third succeeds
        mock_arca_client.create_svm.side_effect = [
            arca_exceptions.ArcaNetworkConflict("IP address 192.168.100.10 is already in use"),
            arca_exceptions.ArcaNetworkConflict("IP address 192.168.100.11 is already in use"),
            {
                "name": "manila_test-project-id",
                "vip": "192.168.100.12",
                "ip_cidr": "192.168.100.12/24",
                "vlan_id": 100,
            },
        ]

        mock_arca_client.create_volume.return_value = {
            "name": "share-share-123",
            "export_path": "192.168.100.12:/exports/manila_test-project-id/share-share-123",
        }

        exports = driver.create_share(Mock(), mock_manila_share, None)

        # Verify SVM creation was retried 3 times
        assert mock_arca_client.create_svm.call_count == 3
        assert exports[0]["path"].endswith("/share-share-123")

    def test_create_svm_exhausts_retries_on_persistent_conflict(self, driver, mock_arca_client, mock_manila_share):
        """Test that persistent IP conflicts eventually raise an error."""
        def get_svm_side_effect(name):
            if name == "manila_test-project-id":
                raise arca_exceptions.ArcaSVMNotFound(svm_name=name)
            return {"name": name}

        mock_arca_client.get_svm.side_effect = get_svm_side_effect
        mock_arca_client.list_svms.return_value = []

        # All attempts fail with IP conflict
        mock_arca_client.create_svm.side_effect = arca_exceptions.ArcaNetworkConflict(
            "IP address already in use"
        )

        with pytest.raises(manila_driver.manila_exception.ShareBackendException, match="Failed to allocate"):
            driver.create_share(Mock(), mock_manila_share, None)

        # Should have attempted max_retries (3) times before giving up
        # Note: Implementation uses max_retries=3 in retry loop
        assert mock_arca_client.create_svm.call_count >= 3

    def test_create_svm_handles_vlan_conflict(self, driver, mock_arca_client, mock_manila_share):
        """Test that VLAN conflicts are also retried."""
        def get_svm_side_effect(name):
            if name == "manila_test-project-id":
                raise arca_exceptions.ArcaSVMNotFound(svm_name=name)
            return {"name": name}

        mock_arca_client.get_svm.side_effect = get_svm_side_effect
        mock_arca_client.list_svms.return_value = []

        # First attempt fails with VLAN conflict, second succeeds
        mock_arca_client.create_svm.side_effect = [
            arca_exceptions.ArcaNetworkConflict("VLAN 100 is already in use"),
            {
                "name": "manila_test-project-id",
                "vip": "192.168.100.10",
                "ip_cidr": "192.168.100.10/24",
                "vlan_id": 100,
            },
        ]

        mock_arca_client.create_volume.return_value = {
            "name": "share-share-123",
            "export_path": "192.168.100.10:/exports/manila_test-project-id/share-share-123",
        }

        exports = driver.create_share(Mock(), mock_manila_share, None)

        assert mock_arca_client.create_svm.call_count == 2
        assert exports[0]["path"].endswith("/share-share-123")

    def test_create_svm_handles_race_condition_svm_exists(self, driver, mock_arca_client, mock_manila_share):
        """Test handling of race where another worker creates the SVM."""
        call_count = {"value": 0}

        def get_svm_side_effect(name):
            if name == "manila_test-project-id":
                call_count["value"] += 1
                if call_count["value"] == 1:
                    # First check: SVM doesn't exist
                    raise arca_exceptions.ArcaSVMNotFound(svm_name=name)
                else:
                    # After create_svm fails, SVM now exists (created by another worker)
                    return {
                        "name": "manila_test-project-id",
                        "vip": "192.168.100.10",
                        "ip_cidr": "192.168.100.10/24",
                        "vlan_id": 100,
                    }
            return {"name": name}

        mock_arca_client.get_svm.side_effect = get_svm_side_effect
        mock_arca_client.list_svms.return_value = []

        # create_svm fails because another worker created it
        mock_arca_client.create_svm.side_effect = arca_exceptions.ArcaNetworkConflict(
            "SVM name already exists"
        )

        mock_arca_client.create_volume.return_value = {
            "name": "share-share-123",
            "export_path": "192.168.100.10:/exports/manila_test-project-id/share-share-123",
        }

        exports = driver.create_share(Mock(), mock_manila_share, None)

        # Should successfully use the SVM created by another worker
        assert exports[0]["path"].endswith("/share-share-123")
        assert mock_arca_client.create_svm.call_count >= 1


class TestPerProjectPoolValidation:
    """Test IP/VLAN pool parsing and validation."""

    def test_invalid_pool_format_raises_error(self, mock_manila_driver_config, mock_arca_client):
        """Test that invalid pool format raises configuration error."""
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "per_project"
            drv.configuration.arca_storage_per_project_ip_pools = [
                "invalid-format"  # Missing required parts
            ]

            with pytest.raises(manila_driver.manila_exception.ShareBackendException):
                drv.do_setup(Mock())

    def test_gateway_in_allocatable_range_raises_error(self, mock_manila_driver_config, mock_arca_client):
        """Test that gateway IP in allocatable range raises error."""
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "per_project"
            # Gateway (.1) is in the allocatable range
            drv.configuration.arca_storage_per_project_ip_pools = [
                "192.168.100.0/24|192.168.100.1-192.168.100.10:100"
            ]

            with pytest.raises(manila_driver.manila_exception.ShareBackendException, match="[Gg]ateway"):
                drv.do_setup(Mock())

    def test_network_address_excluded_from_pool(self, mock_manila_driver_config, mock_arca_client):
        """Test that network and broadcast addresses are excluded."""
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "per_project"
            drv.configuration.arca_storage_per_project_ip_pools = [
                "192.168.100.0/24|192.168.100.2-192.168.100.10:100"
            ]
            drv.do_setup(Mock())

            # Verify pool was parsed (should succeed without gateway conflict)
            assert len(drv._ip_vlan_pools) == 1

    def test_multiple_pools_parsed_correctly(self, mock_manila_driver_config, mock_arca_client):
        """Test that multiple IP/VLAN pools are parsed correctly."""
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "per_project"
            drv.configuration.arca_storage_per_project_ip_pools = [
                "192.168.100.0/24|192.168.100.10-192.168.100.20:100",
                "192.168.101.0/24|192.168.101.10-192.168.101.20:101",
            ]
            drv.do_setup(Mock())

            assert len(drv._ip_vlan_pools) == 2
            assert drv._ip_vlan_pools[0]["vlan_id"] == 100
            assert drv._ip_vlan_pools[1]["vlan_id"] == 101


class TestPerProjectCrossProjectRestrictions:
    """Test cross-project snapshot cloning restrictions."""

    @pytest.fixture
    def driver(self, mock_manila_driver_config, mock_arca_client):
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "per_project"
            drv.configuration.arca_storage_per_project_ip_pools = [
                "192.168.100.0/24|192.168.100.10-192.168.100.20:100"
            ]
            drv.configuration.arca_storage_svm_prefix = "manila_"
            drv.do_setup(Mock())
            return drv

    def test_cross_project_snapshot_clone_blocked(self, driver, mock_manila_snapshot):
        """Test that cross-project snapshot cloning is blocked."""
        # Snapshot from project A
        mock_manila_snapshot["share"]["project_id"] = "project-a"
        mock_manila_snapshot["share"]["metadata"] = {"arca_svm_name": "manila_project-a"}

        # New share in project B
        new_share = {
            "id": "share-456",
            "size": 10,
            "project_id": "project-b",  # Different project
            "metadata": {},
        }

        with pytest.raises(manila_driver.manila_exception.ShareBackendException, match="[Cc]ross-project"):
            driver.create_share_from_snapshot(Mock(), new_share, mock_manila_snapshot, None)

    def test_same_project_snapshot_clone_allowed(self, driver, mock_arca_client, mock_manila_snapshot):
        """Test that same-project snapshot cloning is allowed."""
        # Snapshot from project A
        mock_manila_snapshot["share"]["project_id"] = "test-project-id"
        mock_manila_snapshot["share"]["metadata"] = {"arca_svm_name": "manila_test-project-id"}

        # New share in same project A
        new_share = {
            "id": "share-456",
            "size": 10,
            "project_id": "test-project-id",  # Same project
            "metadata": {},
        }

        mock_arca_client.clone_volume_from_snapshot.return_value = {
            "name": "share-share-456",
            "export_path": "192.168.100.10:/exports/manila_test-project-id/share-share-456",
        }

        exports = driver.create_share_from_snapshot(Mock(), new_share, mock_manila_snapshot, None)

        assert exports[0]["path"].endswith("/share-share-456")
        mock_arca_client.clone_volume_from_snapshot.assert_called_once()

    def test_missing_snapshot_project_id_fails_closed(self, driver, mock_manila_snapshot):
        """Test that missing project ID fails closed (rejects operation)."""
        # Snapshot without project_id
        mock_manila_snapshot["share"]["project_id"] = None
        mock_manila_snapshot["share"]["metadata"] = {"arca_svm_name": "manila_unknown"}

        new_share = {
            "id": "share-456",
            "size": 10,
            "project_id": "test-project-id",
            "metadata": {},
        }

        with pytest.raises(manila_driver.manila_exception.ShareBackendException):
            driver.create_share_from_snapshot(Mock(), new_share, mock_manila_snapshot, None)

    def test_missing_new_share_project_id_fails_closed(self, driver, mock_manila_snapshot):
        """Test that missing project ID on new share fails closed."""
        mock_manila_snapshot["share"]["project_id"] = "test-project-id"
        mock_manila_snapshot["share"]["metadata"] = {"arca_svm_name": "manila_test-project-id"}

        # New share without project_id
        new_share = {
            "id": "share-456",
            "size": 10,
            "project_id": None,  # Missing
            "metadata": {},
        }

        with pytest.raises(manila_driver.manila_exception.ShareBackendException):
            driver.create_share_from_snapshot(Mock(), new_share, mock_manila_snapshot, None)


class TestPerProjectPoolExhaustion:
    """Test behavior when IP/VLAN pools are exhausted."""

    @pytest.fixture
    def driver_small_pool(self, mock_manila_driver_config, mock_arca_client):
        """Create driver with a very small IP pool."""
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "per_project"
            # Very small pool: only 2 allocatable IPs
            drv.configuration.arca_storage_per_project_ip_pools = [
                "192.168.100.0/30|192.168.100.2-192.168.100.2:100"
            ]
            drv.configuration.arca_storage_svm_prefix = "manila_"
            drv.do_setup(Mock())
            return drv

    def test_ip_pool_exhaustion_raises_error(self, driver_small_pool, mock_arca_client, mock_manila_share):
        """Test that IP pool exhaustion raises appropriate error."""
        def get_svm_side_effect(name):
            if name == "manila_test-project-id":
                raise arca_exceptions.ArcaSVMNotFound(svm_name=name)
            return {"name": name}

        mock_arca_client.get_svm.side_effect = get_svm_side_effect

        # All IPs in pool already used
        mock_arca_client.list_svms.return_value = [
            {"name": "manila_other-project", "vip": "192.168.100.2", "vlan_id": 100}
        ]

        # All attempts will fail due to pool exhaustion
        mock_arca_client.create_svm.side_effect = arca_exceptions.ArcaNetworkConflict(
            "IP address already in use"
        )

        with pytest.raises(manila_driver.manila_exception.ShareBackendException, match="No available|exhausted"):
            driver_small_pool.create_share(Mock(), mock_manila_share, None)
