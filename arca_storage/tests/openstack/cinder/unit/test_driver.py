"""Unit tests for ARCA Storage Cinder driver."""

import os
import unittest
from unittest.mock import Mock, patch

import pytest
from cinder import exception

from arca_storage.openstack.cinder import driver as arca_driver
from arca_storage.openstack.cinder import exceptions as arca_exceptions


class TestArcaStorageNFSDriver(unittest.TestCase):
    """Test ArcaStorageNFSDriver's per-SVM NFS file contract."""

    def setUp(self):
        """Set up test fixtures without invoking Cinder's base driver init."""
        self.driver = arca_driver.ArcaStorageNFSDriver.__new__(
            arca_driver.ArcaStorageNFSDriver
        )

        config = Mock()
        config.arca_storage_use_api = True
        config.arca_storage_api_endpoint = "http://192.168.10.5:8080"
        config.arca_storage_api_timeout = 30
        config.arca_storage_api_retry_count = 3
        config.arca_storage_verify_ssl = False
        config.arca_storage_api_auth_type = "none"
        config.arca_storage_api_token = None
        config.arca_storage_driver_ssl_cert_path = None
        config.arca_storage_svm_strategy = "shared"
        config.arca_storage_default_svm = "test-svm"
        config.arca_storage_svm_prefix = "svm-"
        config.arca_storage_nfs_server = "192.168.100.5"
        config.arca_storage_nfs_export_root = "/exports"
        config.arca_storage_nfs_mount_options = "rw,noatime,vers=4.1"
        config.arca_storage_nfs_mount_point_base = "/var/lib/cinder/mnt"
        config.arca_storage_snapshot_copy_timeout = 600
        config.arca_storage_max_over_subscription_ratio = 20.0
        config.reserved_percentage = 0
        config.safe_get.side_effect = lambda key: {
            "volume_backend_name": "arca_storage",
        }.get(key)

        self.driver.configuration = config
        self.driver.arca_client = Mock()
        self.driver._svm_cache = {}
        self.driver._context = Mock(name="context")
        self.driver.db = Mock()
        self.driver._stats = {}

    @patch.object(arca_driver.remotefs_drv.RemoteFSDriver, "do_setup", return_value=None)
    @patch("arca_storage.openstack.cinder.driver.arca_client.ArcaStorageClient")
    def test_do_setup_passes_ssl_cert_path_to_api_client(self, mock_client, mock_super_setup):
        """Configured API CA bundle path is passed through to the REST client."""
        self.driver.configuration.arca_storage_driver_ssl_cert_path = "/etc/ssl/certs/arca-ca.pem"

        self.driver.do_setup(self.driver._context)

        assert mock_client.call_args.kwargs["ca_bundle"] == "/etc/ssl/certs/arca-ca.pem"
        mock_super_setup.assert_called_once_with(self.driver._context)

    @patch.object(arca_driver.remotefs_drv.RemoteFSDriver, "do_setup", return_value=None)
    def test_do_setup_rejects_unimplemented_per_project_strategy(self, mock_super_setup):
        """per_project must fail at backend setup instead of first volume operation."""
        self.driver.configuration.arca_storage_svm_strategy = "per_project"

        with pytest.raises(exception.VolumeBackendAPIException, match="per_project"):
            self.driver.do_setup(self.driver._context)

        mock_super_setup.assert_called_once_with(self.driver._context)

    def _create_mock_volume(self, volume_id="test-vol-id", name="test-volume", size=10):
        """Create a mock Cinder volume object."""
        volume = Mock()
        volume.id = volume_id
        volume.name = name
        volume.size = size
        volume.project_id = "test-project"
        volume.provider_location = None
        volume.volume_type = None
        volume.context = Mock(name=f"context-{volume_id}")
        return volume

    def _create_mock_snapshot(self, snapshot_id="snap-id", volume_id="test-vol-id"):
        """Create a mock Cinder snapshot object."""
        snapshot = Mock()
        snapshot.id = snapshot_id
        snapshot.name = "test-snapshot"
        snapshot.volume_id = volume_id
        snapshot.provider_location = None
        snapshot.provider_id = None
        snapshot.metadata = {}
        snapshot.context = Mock(name=f"context-{snapshot_id}")
        return snapshot

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_volume_creates_file_in_svm_export(self, mock_utils):
        """Create volume mounts the shared SVM export and creates a volume-id file."""
        volume = self._create_mock_volume()
        mount_point = "/var/lib/cinder/mnt/svm_test-svm"
        export_path = "192.168.100.5:/exports/test-svm"

        mock_utils.get_mount_point_for_svm.return_value = mount_point
        mock_utils.create_volume_file.return_value = os.path.join(
            mount_point, "volume-test-vol-id"
        )

        result = self.driver.create_volume(volume)

        assert result == {"provider_location": export_path}
        mock_utils.mount_nfs.assert_called_once_with(
            export_path=export_path,
            mount_point=mount_point,
            mount_options="rw,noatime,vers=4.1",
        )
        mock_utils.create_volume_file.assert_called_once_with(
            mount_point=mount_point,
            volume_name="volume-test-vol-id",
            size_gb=10,
        )
        self.driver.arca_client.create_volume.assert_not_called()
        self.driver.arca_client.create_export.assert_not_called()

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_volume_failure_raises_backend_exception(self, mock_utils):
        """Create volume wraps file creation failures in Cinder's backend exception."""
        volume = self._create_mock_volume()
        mock_utils.get_mount_point_for_svm.return_value = "/var/lib/cinder/mnt/svm_test-svm"
        mock_utils.create_volume_file.side_effect = RuntimeError("file creation failed")

        with pytest.raises(exception.VolumeBackendAPIException):
            self.driver.create_volume(volume)

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_delete_volume_removes_file_without_export_api(self, mock_utils):
        """Delete volume removes only the volume file from the shared SVM export."""
        volume = self._create_mock_volume()
        mount_point = "/var/lib/cinder/mnt/svm_test-svm"
        mock_utils.get_mount_point_for_svm.return_value = mount_point
        mock_utils.is_mounted.return_value = True

        self.driver.delete_volume(volume)

        mock_utils.delete_volume_file.assert_called_once_with(
            mount_point, "volume-test-vol-id"
        )
        mock_utils.unmount_nfs.assert_not_called()
        self.driver.arca_client.delete_export.assert_not_called()
        self.driver.arca_client.delete_volume.assert_not_called()

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_delete_volume_mounts_svm_export_when_needed(self, mock_utils):
        """Delete volume remounts the SVM export after service restart."""
        volume = self._create_mock_volume()
        mount_point = "/var/lib/cinder/mnt/svm_test-svm"
        export_path = "192.168.100.5:/exports/test-svm"
        mock_utils.get_mount_point_for_svm.return_value = mount_point
        mock_utils.is_mounted.return_value = False

        self.driver.delete_volume(volume)

        mock_utils.mount_nfs.assert_called_once_with(
            export_path=export_path,
            mount_point=mount_point,
            mount_options="rw,noatime,vers=4.1",
        )
        mock_utils.delete_volume_file.assert_called_once_with(
            mount_point, "volume-test-vol-id"
        )

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_delete_volume_failure_raises_backend_exception(self, mock_utils):
        """Delete volume reports backend file deletion failures to Cinder."""
        volume = self._create_mock_volume()
        mount_point = "/var/lib/cinder/mnt/svm_test-svm"
        mock_utils.get_mount_point_for_svm.return_value = mount_point
        mock_utils.is_mounted.return_value = True
        mock_utils.delete_volume_file.side_effect = arca_exceptions.ArcaStorageException("delete failed")

        with pytest.raises(exception.VolumeBackendAPIException):
            self.driver.delete_volume(volume)

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_extend_volume_extends_file_in_svm_export(self, mock_utils):
        """Extend volume operates on the volume-id file in the SVM export."""
        volume = self._create_mock_volume()
        mount_point = "/var/lib/cinder/mnt/svm_test-svm"
        mock_utils.get_mount_point_for_svm.return_value = mount_point
        mock_utils.is_mounted.return_value = True

        self.driver.extend_volume(volume, 20)

        mock_utils.extend_volume_file.assert_called_once_with(
            mount_point, "volume-test-vol-id", 20
        )
        self.driver.arca_client.resize_volume.assert_not_called()

    def test_initialize_connection_uses_provider_export_and_volume_id_name(self):
        """Connection info points Nova at the SVM export and volume-id file."""
        volume = self._create_mock_volume()
        volume.provider_location = "192.168.100.5:/exports/test-svm"

        result = self.driver.initialize_connection(volume, {"host": "compute-1"})

        assert result == {
            "driver_volume_type": "nfs",
            "data": {
                "export": "192.168.100.5:/exports/test-svm",
                "name": "volume-test-vol-id",
                "options": "rw,noatime,vers=4.1",
            },
        }
        self.driver.arca_client.get_svm.assert_not_called()

    def test_initialize_connection_regenerates_export_without_provider_location(self):
        """Legacy volumes without provider_location use the shared SVM export."""
        volume = self._create_mock_volume()

        result = self.driver.initialize_connection(volume, {"host": "compute-1"})

        assert result["data"]["export"] == "192.168.100.5:/exports/test-svm"
        assert result["data"]["name"] == "volume-test-vol-id"

    def test_get_svm_for_volume_shared_strategy(self):
        """Shared strategy always uses the configured default SVM."""
        volume = self._create_mock_volume()

        assert self.driver._get_svm_for_volume(volume) == "test-svm"

    def test_get_export_path_uses_configured_nfs_server(self):
        """Static NFS server takes precedence over API lookup."""
        export_path = self.driver._get_export_path("test-svm")

        assert export_path == "192.168.100.5:/exports/test-svm"
        self.driver.arca_client.get_svm.assert_not_called()

    def test_get_export_path_uses_configured_export_root(self):
        """Static NFS mode honors a non-default export root."""
        self.driver.configuration.arca_storage_nfs_export_root = "/srv/arca/exports"

        export_path = self.driver._get_export_path("test-svm")

        assert export_path == "192.168.100.5:/srv/arca/exports/test-svm"
        self.driver.arca_client.get_svm.assert_not_called()

    def test_get_export_path_uses_svm_vip_from_api(self):
        """API mode resolves the SVM VIP and export root when no static server is set."""
        self.driver.configuration.arca_storage_nfs_server = None
        self.driver.arca_client.get_svm.return_value = {
            "name": "test-svm",
            "vip": "192.168.100.9",
            "export_root": "/srv/arca/exports/test-svm",
        }

        export_path = self.driver._get_export_path("test-svm")

        assert export_path == "192.168.100.9:/srv/arca/exports/test-svm"
        self.driver.arca_client.get_svm.assert_called_once_with("test-svm")

    def test_get_export_path_refreshes_api_svm_info(self):
        """API-backed export resolution does not keep using stale SVM data."""
        self.driver.configuration.arca_storage_nfs_server = None
        self.driver.arca_client.get_svm.side_effect = [
            {
                "name": "test-svm",
                "vip": "192.168.100.9",
                "export_root": "/srv/arca/exports/test-svm",
            },
            {
                "name": "test-svm",
                "vip": "192.168.100.10",
                "export_root": "/srv/arca/exports/test-svm",
            },
        ]

        first_export_path = self.driver._get_export_path("test-svm")
        second_export_path = self.driver._get_export_path("test-svm")

        assert first_export_path == "192.168.100.9:/srv/arca/exports/test-svm"
        assert second_export_path == "192.168.100.10:/srv/arca/exports/test-svm"
        assert self.driver.arca_client.get_svm.call_count == 2

    def test_get_export_path_falls_back_to_configured_root_without_api_export_root(self):
        """Older ARCA API responses still work through the configured root fallback."""
        self.driver.configuration.arca_storage_nfs_server = None
        self.driver.configuration.arca_storage_nfs_export_root = "/srv/arca/exports"
        self.driver.arca_client.get_svm.return_value = {
            "name": "test-svm",
            "vip": "192.168.100.9",
        }

        export_path = self.driver._get_export_path("test-svm")

        assert export_path == "192.168.100.9:/srv/arca/exports/test-svm"
        self.driver.arca_client.get_svm.assert_called_once_with("test-svm")

    def test_get_export_path_requires_server_or_api(self):
        """Export path resolution fails when no source can provide the NFS server."""
        self.driver.configuration.arca_storage_nfs_server = None
        self.driver.configuration.arca_storage_use_api = False

        with pytest.raises(exception.VolumeBackendAPIException):
            self.driver._get_export_path("test-svm")

    def test_update_volume_stats_reports_file_backend_capabilities(self):
        """Stats reflect the NFS file backend's supported capabilities."""
        self.driver._update_volume_stats()

        assert self.driver._stats["volume_backend_name"] == "arca_storage"
        assert self.driver._stats["storage_protocol"] == "nfs"
        assert self.driver._stats["snapshot_support"] is True
        assert self.driver._stats["clone_support"] is True
        assert self.driver._stats["thick_provisioning_support"] is False

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_snapshot_copies_volume_file(self, mock_utils):
        """Snapshots are file copies from volume-id to snapshot-id paths."""
        source_volume = self._create_mock_volume(volume_id="source-vol-id")
        snapshot = self._create_mock_snapshot("snap-id", "source-vol-id")
        mount_point = "/var/lib/cinder/mnt/svm_test-svm"
        self.driver.db.volume_get.return_value = source_volume
        mock_utils.get_mount_point_for_svm.return_value = mount_point

        result = self.driver.create_snapshot(snapshot)

        assert result == {
            "provider_location": "192.168.100.5:/exports/test-svm",
            "provider_id": "test-svm",
        }
        mock_utils.copy_sparse_file.assert_called_once_with(
            os.path.join(mount_point, "volume-source-vol-id"),
            os.path.join(mount_point, "snapshot-snap-id"),
            timeout=600,
        )
        self.driver.arca_client.create_snapshot.assert_not_called()

    @patch("arca_storage.openstack.cinder.driver.os.remove")
    @patch("arca_storage.openstack.cinder.driver.os.path.exists", return_value=True)
    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_delete_snapshot_removes_snapshot_file(
        self, mock_utils, mock_exists, mock_remove
    ):
        """Snapshot deletion removes the snapshot-id file from the SVM export."""
        source_volume = self._create_mock_volume(volume_id="source-vol-id")
        snapshot = self._create_mock_snapshot("snap-id", "source-vol-id")
        mount_point = "/var/lib/cinder/mnt/svm_test-svm"
        self.driver.db.volume_get.return_value = source_volume
        mock_utils.get_mount_point_for_svm.return_value = mount_point

        self.driver.delete_snapshot(snapshot)

        mock_remove.assert_called_once_with(os.path.join(mount_point, "snapshot-snap-id"))
        self.driver.arca_client.delete_snapshot.assert_not_called()

    @patch("arca_storage.openstack.cinder.driver.os.remove")
    @patch("arca_storage.openstack.cinder.driver.os.path.exists", return_value=True)
    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_delete_snapshot_uses_provider_fields_without_source_volume(
        self, mock_utils, mock_exists, mock_remove
    ):
        """Driver-managed provider fields are enough to remove a snapshot file."""
        snapshot = self._create_mock_snapshot("snap-id", "missing-source-vol-id")
        snapshot.provider_location = "192.168.100.5:/exports/source-svm"
        snapshot.provider_id = "source-svm"
        mock_utils.get_mount_point_for_svm.return_value = "/var/lib/cinder/mnt/svm_source-svm"
        self.driver.db.volume_get.side_effect = RuntimeError("source volume is gone")

        self.driver.delete_snapshot(snapshot)

        self.driver.db.volume_get.assert_not_called()
        mock_utils.mount_nfs.assert_called_once_with(
            export_path="192.168.100.5:/exports/source-svm",
            mount_point="/var/lib/cinder/mnt/svm_source-svm",
            mount_options="rw,noatime,vers=4.1",
        )
        mock_remove.assert_called_once_with("/var/lib/cinder/mnt/svm_source-svm/snapshot-snap-id")

    @patch("arca_storage.openstack.cinder.driver.os.remove")
    @patch("arca_storage.openstack.cinder.driver.os.path.exists", return_value=True)
    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_delete_snapshot_ignores_user_metadata_for_storage_routing(
        self, mock_utils, mock_exists, mock_remove
    ):
        """User-facing metadata must not control snapshot storage routing."""
        snapshot = self._create_mock_snapshot("snap-id", "missing-source-vol-id")
        snapshot.provider_location = "192.168.100.5:/exports/source-svm"
        snapshot.provider_id = "source-svm"
        snapshot.metadata = {
            "arca_storage:svm_name": "target-svm",
            "arca_storage:export_path": "192.168.100.5:/exports/target-svm",
        }
        mock_utils.get_mount_point_for_svm.return_value = "/var/lib/cinder/mnt/svm_source-svm"
        self.driver.db.volume_get.side_effect = RuntimeError("source volume is gone")

        self.driver.delete_snapshot(snapshot)

        self.driver.db.volume_get.assert_not_called()
        mock_utils.get_mount_point_for_svm.assert_called_once_with(
            "/var/lib/cinder/mnt",
            "source-svm",
        )
        mock_utils.mount_nfs.assert_called_once_with(
            export_path="192.168.100.5:/exports/source-svm",
            mount_point="/var/lib/cinder/mnt/svm_source-svm",
            mount_options="rw,noatime,vers=4.1",
        )
        mock_remove.assert_called_once_with("/var/lib/cinder/mnt/svm_source-svm/snapshot-snap-id")

    @patch("arca_storage.openstack.cinder.driver.os.path.getsize")
    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_volume_from_snapshot_copies_snapshot_file(self, mock_utils, mock_getsize):
        """Create-from-snapshot copies snapshot-id to the new volume-id file."""
        source_volume = self._create_mock_volume(volume_id="source-vol-id")
        new_volume = self._create_mock_volume(volume_id="new-vol-id", name="new-volume", size=20)
        snapshot = self._create_mock_snapshot("snap-id", "source-vol-id")
        mount_point = "/var/lib/cinder/mnt/svm_test-svm"
        self.driver.db.volume_get.return_value = source_volume
        mock_utils.get_mount_point_for_svm.return_value = mount_point
        mock_getsize.return_value = 10 * 1024**3

        result = self.driver.create_volume_from_snapshot(new_volume, snapshot)

        assert result == {"provider_location": "192.168.100.5:/exports/test-svm"}
        mock_utils.copy_sparse_file.assert_called_once_with(
            os.path.join(mount_point, "snapshot-snap-id"),
            os.path.join(mount_point, "volume-new-vol-id"),
            timeout=600,
        )
        mock_utils.extend_volume_file.assert_called_once_with(
            mount_point=mount_point,
            volume_name="volume-new-vol-id",
            new_size_gb=20,
        )
        self.driver.arca_client.create_volume_from_snapshot.assert_not_called()

    @patch("arca_storage.openstack.cinder.driver.os.path.getsize")
    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_volume_from_snapshot_rejects_cross_svm_copy(self, mock_utils, mock_getsize):
        """Manual strategy does not copy snapshot data across SVM boundaries."""
        self.driver.configuration.arca_storage_svm_strategy = "manual"
        source_volume = self._create_mock_volume(volume_id="source-vol-id")
        source_volume.volume_type = {"extra_specs": {"arca_storage:svm_name": "source-svm"}}
        new_volume = self._create_mock_volume(volume_id="new-vol-id", name="new-volume", size=10)
        new_volume.volume_type = {"extra_specs": {"arca_storage:svm_name": "target-svm"}}
        snapshot = self._create_mock_snapshot("snap-id", "source-vol-id")
        self.driver.db.volume_get.return_value = source_volume
        mock_utils.get_mount_point_for_svm.side_effect = lambda base, svm: f"{base}/svm_{svm}"
        mock_getsize.return_value = 10 * 1024**3

        with pytest.raises(exception.VolumeBackendAPIException, match="Cross-SVM"):
            self.driver.create_volume_from_snapshot(new_volume, snapshot)

        mock_utils.mount_nfs.assert_not_called()
        mock_utils.copy_sparse_file.assert_not_called()
        mock_utils.extend_volume_file.assert_not_called()

    @patch("arca_storage.openstack.cinder.driver.os.path.getsize")
    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_volume_from_snapshot_uses_provider_fields_without_source_volume(
        self, mock_utils, mock_getsize
    ):
        """Driver-managed provider fields are enough to restore a snapshot."""
        self.driver.configuration.arca_storage_default_svm = "source-svm"
        new_volume = self._create_mock_volume(volume_id="new-vol-id", name="new-volume", size=10)
        snapshot = self._create_mock_snapshot("snap-id", "missing-source-vol-id")
        snapshot.provider_location = "192.168.100.5:/exports/source-svm"
        snapshot.provider_id = "source-svm"
        mock_utils.get_mount_point_for_svm.return_value = "/var/lib/cinder/mnt/svm_source-svm"
        mock_getsize.return_value = 10 * 1024**3
        self.driver.db.volume_get.side_effect = RuntimeError("source volume is gone")

        result = self.driver.create_volume_from_snapshot(new_volume, snapshot)

        assert result == {"provider_location": "192.168.100.5:/exports/source-svm"}
        self.driver.db.volume_get.assert_not_called()
        mock_utils.copy_sparse_file.assert_called_once_with(
            "/var/lib/cinder/mnt/svm_source-svm/snapshot-snap-id",
            "/var/lib/cinder/mnt/svm_source-svm/volume-new-vol-id",
            timeout=600,
        )
        mock_utils.extend_volume_file.assert_not_called()

    @patch("arca_storage.openstack.cinder.driver.os.path.getsize")
    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_volume_from_snapshot_ignores_user_metadata_for_storage_routing(
        self, mock_utils, mock_getsize
    ):
        """User-facing metadata must not bypass cross-SVM restore protection."""
        self.driver.configuration.arca_storage_svm_strategy = "manual"
        new_volume = self._create_mock_volume(volume_id="new-vol-id", name="new-volume", size=10)
        new_volume.volume_type = {"extra_specs": {"arca_storage:svm_name": "source-svm"}}
        snapshot = self._create_mock_snapshot("snap-id", "missing-source-vol-id")
        snapshot.provider_location = "192.168.100.5:/exports/source-svm"
        snapshot.provider_id = "source-svm"
        snapshot.metadata = {
            "arca_storage:svm_name": "target-svm",
            "arca_storage:export_path": "192.168.100.5:/exports/target-svm",
        }
        mock_utils.get_mount_point_for_svm.return_value = "/var/lib/cinder/mnt/svm_source-svm"
        mock_getsize.return_value = 10 * 1024**3
        self.driver.db.volume_get.side_effect = RuntimeError("source volume is gone")

        result = self.driver.create_volume_from_snapshot(new_volume, snapshot)

        assert result == {"provider_location": "192.168.100.5:/exports/source-svm"}
        self.driver.db.volume_get.assert_not_called()
        mock_utils.get_mount_point_for_svm.assert_called_once_with(
            "/var/lib/cinder/mnt",
            "source-svm",
        )
        mock_utils.copy_sparse_file.assert_called_once_with(
            "/var/lib/cinder/mnt/svm_source-svm/snapshot-snap-id",
            "/var/lib/cinder/mnt/svm_source-svm/volume-new-vol-id",
            timeout=600,
        )
        mock_utils.extend_volume_file.assert_not_called()

    @patch("arca_storage.openstack.cinder.driver.os.remove")
    @patch("arca_storage.openstack.cinder.driver.os.path.getsize")
    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_volume_from_snapshot_cleans_up_file_after_post_copy_failure(
        self, mock_utils, mock_getsize, mock_remove
    ):
        """Post-copy failures remove the newly-created destination file."""
        source_volume = self._create_mock_volume(volume_id="source-vol-id")
        new_volume = self._create_mock_volume(volume_id="new-vol-id", name="new-volume", size=20)
        snapshot = self._create_mock_snapshot("snap-id", "source-vol-id")
        mount_point = "/var/lib/cinder/mnt/svm_test-svm"
        self.driver.db.volume_get.return_value = source_volume
        mock_utils.get_mount_point_for_svm.return_value = mount_point
        mock_getsize.return_value = 10 * 1024**3
        mock_utils.extend_volume_file.side_effect = RuntimeError("extend failed")

        with pytest.raises(exception.VolumeBackendAPIException):
            self.driver.create_volume_from_snapshot(new_volume, snapshot)

        mock_remove.assert_called_once_with(os.path.join(mount_point, "volume-new-vol-id"))

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_cloned_volume_copies_source_volume_file(self, mock_utils):
        """Clone creates a new volume-id file by copying the source volume-id file."""
        source_volume = self._create_mock_volume(volume_id="source-vol-id", size=10)
        new_volume = self._create_mock_volume(volume_id="clone-vol-id", size=12)
        mount_point = "/var/lib/cinder/mnt/svm_test-svm"
        mock_utils.get_mount_point_for_svm.return_value = mount_point

        result = self.driver.create_cloned_volume(new_volume, source_volume)

        assert result == {"provider_location": "192.168.100.5:/exports/test-svm"}
        mock_utils.copy_sparse_file.assert_called_once_with(
            os.path.join(mount_point, "volume-source-vol-id"),
            os.path.join(mount_point, "volume-clone-vol-id"),
            timeout=600,
        )
        mock_utils.extend_volume_file.assert_called_once_with(
            mount_point=mount_point,
            volume_name="volume-clone-vol-id",
            new_size_gb=12,
        )
        self.driver.arca_client.create_snapshot.assert_not_called()

    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_cloned_volume_rejects_cross_svm_copy(self, mock_utils):
        """Manual strategy does not clone volume data across SVM boundaries."""
        self.driver.configuration.arca_storage_svm_strategy = "manual"
        source_volume = self._create_mock_volume(volume_id="source-vol-id", size=10)
        source_volume.volume_type = {"extra_specs": {"arca_storage:svm_name": "source-svm"}}
        new_volume = self._create_mock_volume(volume_id="clone-vol-id", size=10)
        new_volume.volume_type = {"extra_specs": {"arca_storage:svm_name": "target-svm"}}
        mock_utils.get_mount_point_for_svm.side_effect = lambda base, svm: f"{base}/svm_{svm}"

        with pytest.raises(exception.VolumeBackendAPIException, match="Cross-SVM"):
            self.driver.create_cloned_volume(new_volume, source_volume)

        mock_utils.mount_nfs.assert_not_called()
        mock_utils.copy_sparse_file.assert_not_called()
        mock_utils.extend_volume_file.assert_not_called()

    @patch("arca_storage.openstack.cinder.driver.os.remove")
    @patch("arca_storage.openstack.cinder.driver.arca_utils")
    def test_create_cloned_volume_cleans_up_file_after_post_copy_failure(self, mock_utils, mock_remove):
        """Post-copy failures remove the newly-created cloned file."""
        source_volume = self._create_mock_volume(volume_id="source-vol-id", size=10)
        new_volume = self._create_mock_volume(volume_id="clone-vol-id", size=12)
        mount_point = "/var/lib/cinder/mnt/svm_test-svm"
        mock_utils.get_mount_point_for_svm.return_value = mount_point
        mock_utils.extend_volume_file.side_effect = RuntimeError("extend failed")

        with pytest.raises(exception.VolumeBackendAPIException):
            self.driver.create_cloned_volume(new_volume, source_volume)

        mock_remove.assert_called_once_with(os.path.join(mount_point, "volume-clone-vol-id"))

    def test_get_qos_specs_no_volume_type(self):
        """QoS extraction returns no specs when the volume has no type."""
        volume = self._create_mock_volume()

        assert self.driver._get_qos_specs(volume) == {}

    def test_get_qos_specs_with_total_iops(self):
        """Total IOPS applies to read and write when specific limits are absent."""
        volume = self._create_mock_volume()
        volume.volume_type = {
            "extra_specs": {
                "arca_storage:total_iops_sec": "4000",
            }
        }

        qos_specs = self.driver._get_qos_specs(volume)

        assert qos_specs["read_iops"] == 4000
        assert qos_specs["write_iops"] == 4000

    def test_get_qos_specs_with_bandwidth(self):
        """QoS extraction maps Cinder byte/sec specs to ARCA fields."""
        volume = self._create_mock_volume()
        volume.volume_type = {
            "extra_specs": {
                "arca_storage:read_bytes_sec": "524288000",
                "arca_storage:write_bytes_sec": "314572800",
            }
        }

        qos_specs = self.driver._get_qos_specs(volume)

        assert qos_specs["read_bps"] == 524288000
        assert qos_specs["write_bps"] == 314572800

    def test_apply_qos_to_volume_no_specs(self):
        """QoS apply is skipped when the volume type has no relevant specs."""
        volume = self._create_mock_volume()

        self.driver._apply_qos_to_volume(volume)

        self.driver.arca_client.apply_qos.assert_not_called()

    def test_apply_qos_to_volume_with_specs(self):
        """QoS specs are not sent to ARCA API for file-backed Cinder volumes."""
        volume = self._create_mock_volume()
        volume.volume_type = {
            "extra_specs": {
                "arca_storage:read_iops_sec": "5000",
                "arca_storage:write_iops_sec": "5000",
            }
        }

        self.driver._apply_qos_to_volume(volume)

        self.driver.arca_client.apply_qos.assert_not_called()

    def test_retype_skips_file_backed_qos_changes(self):
        """Retype accepts QoS specs but cannot apply them to file-backed volumes."""
        volume = self._create_mock_volume()
        volume.volume_type = {"extra_specs": {"arca_storage:read_iops_sec": "3000"}}
        new_type = {
            "name": "gold",
            "extra_specs": {
                "arca_storage:read_iops_sec": "5000",
                "arca_storage:write_iops_sec": "5000",
            },
        }
        diff = {"extra_specs": {"arca_storage:read_iops_sec": ("3000", "5000")}}

        changed, updates = self.driver.retype(None, volume, new_type, diff, None)

        assert changed is True
        assert updates == {}
        self.driver.arca_client.apply_qos.assert_not_called()

    def test_retype_rejects_manual_svm_changes(self):
        """Retype cannot change the SVM that owns an existing backing file."""
        self.driver.configuration.arca_storage_svm_strategy = "manual"
        volume = self._create_mock_volume()
        volume.volume_type = {
            "extra_specs": {"arca_storage:svm_name": "source-svm"}
        }
        new_type = {
            "name": "target-type",
            "extra_specs": {"arca_storage:svm_name": "target-svm"},
        }
        diff = {
            "extra_specs": {
                "arca_storage:svm_name": ("source-svm", "target-svm")
            }
        }

        changed, updates = self.driver.retype(None, volume, new_type, diff, None)

        assert changed is False
        assert updates == {}
        self.driver.arca_client.apply_qos.assert_not_called()

    def test_retype_rejects_manual_missing_svm_specs(self):
        """Manual retype fails closed when SVM placement cannot be compared."""
        self.driver.configuration.arca_storage_svm_strategy = "manual"
        volume = self._create_mock_volume()
        volume.volume_type = None
        new_type = {
            "name": "gold",
            "extra_specs": {"arca_storage:read_iops_sec": "5000"},
        }
        diff = {"extra_specs": {"arca_storage:read_iops_sec": (None, "5000")}}

        changed, updates = self.driver.retype(None, volume, new_type, diff, None)

        assert changed is False
        assert updates == {}
        self.driver.arca_client.apply_qos.assert_not_called()

    def test_retype_allows_manual_qos_change_when_svm_preserved(self):
        """Manual retype can update non-placement specs when SVM stays stable."""
        self.driver.configuration.arca_storage_svm_strategy = "manual"
        volume = self._create_mock_volume()
        volume.volume_type = {
            "extra_specs": {
                "arca_storage:svm_name": "source-svm",
                "arca_storage:read_iops_sec": "3000",
            }
        }
        new_type = {
            "name": "gold",
            "extra_specs": {
                "arca_storage:svm_name": "source-svm",
                "arca_storage:read_iops_sec": "5000",
            },
        }
        diff = {"extra_specs": {"arca_storage:read_iops_sec": ("3000", "5000")}}

        changed, updates = self.driver.retype(None, volume, new_type, diff, None)

        assert changed is True
        assert updates == {}
        self.driver.arca_client.apply_qos.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
