"""Unit tests for ARCA Storage Cinder driver."""

import unittest
from unittest.mock import MagicMock, Mock, patch, PropertyMock

import pytest

from arca_storage.openstack.cinder import driver as arca_driver
from arca_storage.openstack.cinder import exceptions as arca_exceptions


class TestArcaStorageNFSDriver(unittest.TestCase):
    """Test ArcaStorageNFSDriver class."""

    def setUp(self):
        """Set up test fixtures."""
        self.driver = arca_driver.ArcaStorageNFSDriver()

        # Mock configuration
        self.driver.configuration = Mock()
        self.driver.configuration.arca_storage_api_endpoint = "http://192.168.10.5:8080"
        self.driver.configuration.arca_storage_api_timeout = 30
        self.driver.configuration.arca_storage_api_retry_count = 3
        self.driver.configuration.arca_storage_verify_ssl = False
        self.driver.configuration.arca_storage_svm_strategy = "shared"
        self.driver.configuration.arca_storage_default_svm = "test-svm"
        self.driver.configuration.arca_storage_nfs_mount_options = "rw,noatime,vers=4.1"
        self.driver.configuration.arca_storage_nfs_mount_point_base = "/var/lib/cinder/mnt"
        self.driver.configuration.arca_storage_thin_provisioning = True
        self.driver.configuration.arca_storage_client_cidr = "10.0.0.0/16"

        # Mock ARCA client
        self.driver.arca_client = Mock()

    def _create_mock_volume(self, volume_id="test-vol-id", name="test-volume", size=10):
        """Create a mock volume object."""
        volume = Mock()
        volume.id = volume_id
        volume.name = name
        volume.size = size
        volume.project_id = "test-project"
        volume.provider_location = None
        return volume

    @patch("arca_storage.openstack.cinder.driver.arca_client.ArcaStorageClient")
    def test_do_setup(self, mock_client_class):
        """Test driver setup."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        driver = arca_driver.ArcaStorageNFSDriver()
        driver.configuration = self.driver.configuration

        driver.do_setup(None)

        assert driver.arca_client is not None
        mock_client_class.assert_called_once()

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_volume_success(self, mock_utils):
        """Test successful volume creation."""
        volume = self._create_mock_volume()

        # Mock API responses
        self.driver.arca_client.create_volume.return_value = {
            "name": "test-volume",
            "status": "available",
        }
        self.driver.arca_client.create_export.return_value = {
            "svm": "test-svm",
            "volume": "test-volume",
        }
        self.driver.arca_client.get_svm.return_value = {
            "name": "test-svm",
            "vip": "192.168.100.5",
        }

        # Mock utils
        mock_utils.get_export_path.return_value = "192.168.100.5:/exports/test-svm/test-volume"
        mock_utils.get_mount_point_for_volume.return_value = "/var/lib/cinder/mnt/test"
        mock_utils.create_volume_file.return_value = "/var/lib/cinder/mnt/test/test-volume"

        result = self.driver.create_volume(volume)

        assert "provider_location" in result
        assert result["provider_location"] == "192.168.100.5:/exports/test-svm/test-volume"

        self.driver.arca_client.create_volume.assert_called_once()
        self.driver.arca_client.create_export.assert_called_once()
        mock_utils.mount_nfs.assert_called_once()
        mock_utils.create_volume_file.assert_called_once()

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_volume_failure_with_cleanup(self, mock_utils):
        """Test volume creation failure triggers cleanup."""
        volume = self._create_mock_volume()

        # Mock API responses
        self.driver.arca_client.create_volume.return_value = {"name": "test-volume"}
        self.driver.arca_client.create_export.return_value = {"svm": "test-svm"}
        self.driver.arca_client.get_svm.return_value = {"name": "test-svm", "vip": "192.168.100.5"}

        # Mock utils
        mock_utils.get_export_path.return_value = "192.168.100.5:/exports/test-svm/test-volume"
        mock_utils.get_mount_point_for_volume.return_value = "/var/lib/cinder/mnt/test"
        mock_utils.create_volume_file.side_effect = Exception("File creation failed")

        with patch.object(self.driver, "_cleanup_failed_volume") as mock_cleanup:
            with pytest.raises(Exception):
                self.driver.create_volume(volume)

            # Verify cleanup was called
            mock_cleanup.assert_called_once()
            cleanup_state = mock_cleanup.call_args[0][1]
            assert cleanup_state["volume_created"] is True
            assert cleanup_state["export_created"] is True
            assert cleanup_state["mounted"] is True

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_delete_volume_success(self, mock_utils):
        """Test successful volume deletion."""
        volume = self._create_mock_volume()

        mock_utils.get_mount_point_for_volume.return_value = "/var/lib/cinder/mnt/test"
        mock_utils.is_mounted.return_value = True

        self.driver.delete_volume(volume)

        mock_utils.delete_volume_file.assert_called_once()
        mock_utils.unmount_nfs.assert_called_once()
        self.driver.arca_client.delete_export.assert_called_once()
        self.driver.arca_client.delete_volume.assert_called_once()

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_extend_volume_success(self, mock_utils):
        """Test successful volume extension."""
        volume = self._create_mock_volume()

        self.driver.arca_client.resize_volume.return_value = {"size_gib": 20}
        self.driver.arca_client.get_svm.return_value = {"name": "test-svm", "vip": "192.168.100.5"}

        mock_utils.get_mount_point_for_volume.return_value = "/var/lib/cinder/mnt/test"
        mock_utils.is_mounted.return_value = True

        self.driver.extend_volume(volume, 20)

        self.driver.arca_client.resize_volume.assert_called_once()
        mock_utils.extend_volume_file.assert_called_once()

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_extend_volume_with_mount(self, mock_utils):
        """Test volume extension with temporary mount."""
        volume = self._create_mock_volume()

        self.driver.arca_client.resize_volume.return_value = {"size_gib": 20}
        self.driver.arca_client.get_svm.return_value = {"name": "test-svm", "vip": "192.168.100.5"}

        mock_utils.get_mount_point_for_volume.return_value = "/var/lib/cinder/mnt/test"
        mock_utils.is_mounted.return_value = False
        mock_utils.get_export_path.return_value = "192.168.100.5:/exports/test-svm/test-volume"

        self.driver.extend_volume(volume, 20)

        # Should mount, extend, then unmount
        mock_utils.mount_nfs.assert_called_once()
        mock_utils.extend_volume_file.assert_called_once()
        mock_utils.unmount_nfs.assert_called_once()

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_initialize_connection_with_provider_location(self, mock_utils):
        """Test connection initialization with provider_location."""
        volume = self._create_mock_volume()
        volume.provider_location = "192.168.100.5:/exports/test-svm/test-volume"

        connector = {"host": "compute-node-1"}

        result = self.driver.initialize_connection(volume, connector)

        assert result["driver_volume_type"] == "nfs"
        assert result["data"]["export"] == "192.168.100.5:/exports/test-svm/test-volume"
        assert result["data"]["name"] == "test-volume"

        # Should NOT call get_svm when provider_location exists
        self.driver.arca_client.get_svm.assert_not_called()

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_initialize_connection_without_provider_location(self, mock_utils):
        """Test connection initialization without provider_location (fallback)."""
        volume = self._create_mock_volume()
        volume.provider_location = None

        connector = {"host": "compute-node-1"}

        self.driver.arca_client.get_svm.return_value = {"name": "test-svm", "vip": "192.168.100.5"}
        mock_utils.get_export_path.return_value = "192.168.100.5:/exports/test-svm/test-volume"

        result = self.driver.initialize_connection(volume, connector)

        assert result["driver_volume_type"] == "nfs"
        assert result["data"]["export"] == "192.168.100.5:/exports/test-svm/test-volume"

        # Should call get_svm for fallback
        self.driver.arca_client.get_svm.assert_called_once()

    def test_terminate_connection(self):
        """Test connection termination."""
        volume = self._create_mock_volume()
        connector = {"host": "compute-node-1"}

        # Should not raise exception
        self.driver.terminate_connection(volume, connector)

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_cleanup_failed_volume_all_states(self, mock_utils):
        """Test cleanup with all resources created."""
        cleanup_state = {
            "svm_name": "test-svm",
            "volume_created": True,
            "export_created": True,
            "mount_point": "/var/lib/cinder/mnt/test",
            "mounted": True,
        }

        self.driver._cleanup_failed_volume("test-volume", cleanup_state)

        mock_utils.unmount_nfs.assert_called_once()
        mock_utils.cleanup_mount_point.assert_called_once()
        self.driver.arca_client.delete_export.assert_called_once()
        self.driver.arca_client.delete_volume.assert_called_once()

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_cleanup_failed_volume_partial_states(self, mock_utils):
        """Test cleanup with partial resource creation."""
        cleanup_state = {
            "svm_name": "test-svm",
            "volume_created": True,
            "export_created": False,
            "mount_point": "/var/lib/cinder/mnt/test",
            "mounted": False,
        }

        self.driver._cleanup_failed_volume("test-volume", cleanup_state)

        # Should cleanup mount point even if not mounted
        mock_utils.cleanup_mount_point.assert_called_once()
        mock_utils.unmount_nfs.assert_not_called()

        # Should not try to delete export
        self.driver.arca_client.delete_export.assert_not_called()

        # Should delete volume
        self.driver.arca_client.delete_volume.assert_called_once()

    def test_cleanup_failed_volume_no_svm_name(self):
        """Test cleanup without SVM name."""
        cleanup_state = {
            "svm_name": None,
        }

        # Should return without error
        self.driver._cleanup_failed_volume("test-volume", cleanup_state)

        self.driver.arca_client.delete_volume.assert_not_called()

    def test_get_svm_for_volume_shared_strategy(self):
        """Test SVM selection with shared strategy."""
        volume = self._create_mock_volume()

        svm_name = self.driver._get_svm_for_volume(volume)

        assert svm_name == "test-svm"

    def test_get_svm_info_with_cache(self):
        """Test SVM info retrieval with caching."""
        self.driver.arca_client.get_svm.return_value = {
            "name": "test-svm",
            "vip": "192.168.100.5",
        }

        # First call
        result1 = self.driver._get_svm_info("test-svm")
        assert result1["name"] == "test-svm"

        # Second call should use cache
        result2 = self.driver._get_svm_info("test-svm")
        assert result2["name"] == "test-svm"

        # Should only call API once
        self.driver.arca_client.get_svm.assert_called_once()

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_get_volume_stats(self, mock_utils):
        """Test volume stats retrieval."""
        self.driver.configuration.arca_storage_volume_backend_name = "arca_storage"
        self.driver.configuration.arca_storage_max_over_subscription_ratio = 20.0

        # Mock SVM info
        self.driver.arca_client.get_svm.return_value = {
            "name": "test-svm",
            "total_capacity_gib": 1000,
            "used_capacity_gib": 200,
        }

        stats = self.driver.get_volume_stats(refresh=True)

        assert stats["volume_backend_name"] == "arca_storage"
        assert stats["vendor_name"] == "ARCA Storage"
        assert stats["driver_version"] is not None
        assert stats["storage_protocol"] == "nfs"

    def test_check_for_setup_error_svm_not_found(self):
        """Test setup error check when SVM not found."""
        self.driver.arca_client.get_svm.side_effect = arca_exceptions.ArcaSVMNotFound(
            "SVM not found"
        )

        with pytest.raises(Exception, match="not found"):
            self.driver.check_for_setup_error()

    # Snapshot tests

    def test_create_snapshot_success(self):
        """Test successful snapshot creation."""
        volume = self._create_mock_volume()
        snapshot = Mock()
        snapshot.name = "test-snapshot"
        snapshot.volume = volume

        self.driver.arca_client.create_snapshot.return_value = {
            "name": "test-snapshot",
            "svm": "test-svm",
            "volume": "test-volume",
            "status": "available",
        }

        result = self.driver.create_snapshot(snapshot)

        assert result == {}
        self.driver.arca_client.create_snapshot.assert_called_once_with(
            name="test-snapshot",
            svm="test-svm",
            volume="test-volume",
        )

    def test_delete_snapshot_success(self):
        """Test successful snapshot deletion."""
        volume = self._create_mock_volume()
        snapshot = Mock()
        snapshot.name = "test-snapshot"
        snapshot.volume = volume

        self.driver.delete_snapshot(snapshot)

        self.driver.arca_client.delete_snapshot.assert_called_once_with(
            name="test-snapshot",
            svm="test-svm",
            volume="test-volume",
            force=True,
        )

    def test_delete_snapshot_not_found(self):
        """Test snapshot deletion when snapshot not found."""
        volume = self._create_mock_volume()
        snapshot = Mock()
        snapshot.name = "test-snapshot"
        snapshot.volume = volume

        self.driver.arca_client.delete_snapshot.side_effect = arca_exceptions.ArcaSnapshotNotFound(
            "Snapshot not found"
        )

        # Should not raise exception (snapshot already deleted)
        self.driver.delete_snapshot(snapshot)

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_volume_from_snapshot_success(self, mock_utils):
        """Test successful volume creation from snapshot."""
        # Source volume and snapshot
        source_volume = self._create_mock_volume(
            volume_id="source-vol-id",
            name="source-volume",
            size=10,
        )
        snapshot = Mock()
        snapshot.name = "test-snapshot"
        snapshot.volume = source_volume

        # New volume
        new_volume = self._create_mock_volume(
            volume_id="new-vol-id",
            name="new-volume",
            size=10,
        )

        # Mock API responses
        self.driver.arca_client.create_volume_from_snapshot.return_value = {
            "name": "new-volume",
            "svm": "test-svm",
            "size_gib": 10,
            "status": "available",
        }
        self.driver.arca_client.create_export.return_value = {
            "svm": "test-svm",
            "volume": "new-volume",
        }
        self.driver.arca_client.get_svm.return_value = {
            "name": "test-svm",
            "vip": "192.168.100.5",
        }

        # Mock utils
        mock_utils.get_export_path.return_value = "192.168.100.5:/exports/test-svm/new-volume"
        mock_utils.get_mount_point_for_volume.return_value = "/var/lib/cinder/mnt/test"
        mock_utils.create_volume_file.return_value = "/var/lib/cinder/mnt/test/new-volume"

        result = self.driver.create_volume_from_snapshot(new_volume, snapshot)

        assert "provider_location" in result
        assert result["provider_location"] == "192.168.100.5:/exports/test-svm/new-volume"

        self.driver.arca_client.create_volume_from_snapshot.assert_called_once_with(
            name="new-volume",
            svm="test-svm",
            snapshot="test-snapshot",
            size_gib=10,
        )
        self.driver.arca_client.create_export.assert_called_once()
        mock_utils.mount_nfs.assert_called_once()
        mock_utils.create_volume_file.assert_called_once()

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_cloned_volume_success(self, mock_utils):
        """Test successful volume cloning."""
        # Source volume
        source_volume = self._create_mock_volume(
            volume_id="source-vol-id",
            name="source-volume",
            size=10,
        )

        # New volume (clone)
        new_volume = self._create_mock_volume(
            volume_id="clone-vol-id",
            name="clone-volume",
            size=10,
        )

        # Mock API responses
        self.driver.arca_client.create_snapshot.return_value = {
            "name": "temp_clone_clone-volume",
            "svm": "test-svm",
            "volume": "source-volume",
        }
        self.driver.arca_client.create_volume_from_snapshot.return_value = {
            "name": "clone-volume",
            "svm": "test-svm",
            "size_gib": 10,
        }
        self.driver.arca_client.create_export.return_value = {
            "svm": "test-svm",
            "volume": "clone-volume",
        }
        self.driver.arca_client.get_svm.return_value = {
            "name": "test-svm",
            "vip": "192.168.100.5",
        }

        # Mock utils
        mock_utils.get_export_path.return_value = "192.168.100.5:/exports/test-svm/clone-volume"
        mock_utils.get_mount_point_for_volume.return_value = "/var/lib/cinder/mnt/test"
        mock_utils.create_volume_file.return_value = "/var/lib/cinder/mnt/test/clone-volume"

        result = self.driver.create_cloned_volume(new_volume, source_volume)

        assert "provider_location" in result

        # Verify temporary snapshot was created
        self.driver.arca_client.create_snapshot.assert_called_once_with(
            name="temp_clone_clone-volume",
            svm="test-svm",
            volume="source-volume",
        )

        # Verify volume was created from snapshot
        self.driver.arca_client.create_volume_from_snapshot.assert_called_once()

        # Verify temporary snapshot was deleted
        self.driver.arca_client.delete_snapshot.assert_called_once_with(
            name="temp_clone_clone-volume",
            svm="test-svm",
            volume="source-volume",
            force=True,
        )

    # QoS tests

    def test_get_qos_specs_no_volume_type(self):
        """Test QoS spec extraction with no volume type."""
        volume = self._create_mock_volume()
        volume.volume_type = None

        qos_specs = self.driver._get_qos_specs(volume)

        assert qos_specs == {}

    def test_get_qos_specs_with_iops(self):
        """Test QoS spec extraction with IOPS limits."""
        volume = self._create_mock_volume()

        class MockVolumeType:
            extra_specs = {
                "arca_storage:read_iops_sec": "5000",
                "arca_storage:write_iops_sec": "3000",
            }

        volume.volume_type = MockVolumeType()

        qos_specs = self.driver._get_qos_specs(volume)

        assert qos_specs["read_iops"] == 5000
        assert qos_specs["write_iops"] == 3000

    def test_get_qos_specs_with_total_iops(self):
        """Test QoS spec extraction with total IOPS."""
        volume = self._create_mock_volume()

        class MockVolumeType:
            extra_specs = {
                "arca_storage:total_iops_sec": "4000",
            }

        volume.volume_type = MockVolumeType()

        qos_specs = self.driver._get_qos_specs(volume)

        # total_iops applies to both read and write
        assert qos_specs["read_iops"] == 4000
        assert qos_specs["write_iops"] == 4000

    def test_get_qos_specs_with_bandwidth(self):
        """Test QoS spec extraction with bandwidth limits."""
        volume = self._create_mock_volume()

        class MockVolumeType:
            extra_specs = {
                "arca_storage:read_bytes_sec": "524288000",
                "arca_storage:write_bytes_sec": "314572800",
            }

        volume.volume_type = MockVolumeType()

        qos_specs = self.driver._get_qos_specs(volume)

        assert qos_specs["read_bps"] == 524288000
        assert qos_specs["write_bps"] == 314572800

    def test_apply_qos_to_volume_no_specs(self):
        """Test QoS application with no specs."""
        volume = self._create_mock_volume()
        volume.volume_type = None

        # Should not raise exception
        self.driver._apply_qos_to_volume(volume)

        # Should not call apply_qos
        self.driver.arca_client.apply_qos.assert_not_called()

    def test_apply_qos_to_volume_with_specs(self):
        """Test QoS application with specs."""
        volume = self._create_mock_volume()

        class MockVolumeType:
            extra_specs = {
                "arca_storage:read_iops_sec": "5000",
                "arca_storage:write_iops_sec": "5000",
            }

        volume.volume_type = MockVolumeType()

        self.driver._apply_qos_to_volume(volume)

        self.driver.arca_client.apply_qos.assert_called_once_with(
            volume="test-volume",
            svm="test-svm",
            read_iops=5000,
            write_iops=5000,
            read_bps=None,
            write_bps=None,
        )

    def test_retype_qos_change(self):
        """Test retype with QoS changes."""
        volume = self._create_mock_volume()

        # Old volume type
        class OldVolumeType:
            extra_specs = {
                "arca_storage:read_iops_sec": "3000",
            }

        volume.volume_type = OldVolumeType()

        # New volume type
        new_type = {
            "name": "gold",
            "extra_specs": {
                "arca_storage:read_iops_sec": "5000",
                "arca_storage:write_iops_sec": "5000",
            },
        }

        diff = {
            "extra_specs": {
                "arca_storage:read_iops_sec": ("3000", "5000"),
                "arca_storage:write_iops_sec": (None, "5000"),
            }
        }

        changed, updates = self.driver.retype(
            context=None,
            volume=volume,
            new_type=new_type,
            diff=diff,
            host=None,
        )

        assert changed is True
        assert updates == {}
        self.driver.arca_client.apply_qos.assert_called_once()

    def test_retype_no_qos_change(self):
        """Test retype with no QoS changes."""
        volume = self._create_mock_volume()
        volume.volume_type = None

        new_type = {
            "name": "standard",
            "extra_specs": {},
        }

        diff = {}

        changed, updates = self.driver.retype(
            context=None,
            volume=volume,
            new_type=new_type,
            diff=diff,
            host=None,
        )

        assert changed is True
        assert updates == {}
        # apply_qos may be called but with no specs
        # or not called at all


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
