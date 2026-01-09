"""Unit tests for Manila driver idempotency behavior.

These tests verify that operations are idempotent and handle retry scenarios correctly.
"""

from unittest.mock import Mock, patch

import pytest

from arca_storage.openstack.manila import driver as manila_driver
from arca_storage.openstack.manila import exceptions as arca_exceptions


class TestShareCreationIdempotency:
    """Test idempotent share creation behavior."""

    @pytest.fixture
    def driver_shared(self, mock_manila_driver_config, mock_arca_client):
        """Create driver with shared strategy."""
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "shared"
            drv.configuration.arca_storage_default_svm = "test-svm"
            drv.do_setup(Mock())
            return drv

    def test_create_share_already_exists_returns_existing(self, driver_shared, mock_arca_client, mock_manila_share):
        """Test that creating existing share returns existing export."""
        # First call fails with "already exists"
        mock_arca_client.create_volume.side_effect = arca_exceptions.ArcaShareAlreadyExists(
            share_id="share-share-123"
        )

        # get_volume returns the existing share
        mock_arca_client.get_volume.return_value = {
            "name": "share-share-123",
            "export_path": "192.168.100.5:/exports/test-svm/share-share-123",
        }

        exports = driver_shared.create_share(Mock(), mock_manila_share, None)

        # Should return the existing share's export
        assert len(exports) == 1
        assert exports[0]["path"] == "192.168.100.5:/exports/test-svm/share-share-123"

        # Should have tried to create, then fetched existing
        mock_arca_client.create_volume.assert_called_once()
        mock_arca_client.get_volume.assert_called_once()

    def test_create_share_already_exists_without_export_raises(self, driver_shared, mock_arca_client, mock_manila_share):
        """Test that share already exists without export path raises error."""
        mock_arca_client.create_volume.side_effect = arca_exceptions.ArcaShareAlreadyExists(
            share_id="share-share-123"
        )

        # Existing share doesn't have export_path
        mock_arca_client.get_volume.return_value = {
            "name": "share-share-123",
            # No export_path - share exists but isn't exported yet
        }

        with pytest.raises(manila_driver.manila_exception.ShareBackendException, match="[Ee]xists.*not exported"):
            driver_shared.create_share(Mock(), mock_manila_share, None)

    @pytest.fixture
    def driver_per_project(self, mock_manila_driver_config, mock_arca_client):
        """Create driver with per_project strategy."""
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

    def test_create_share_per_project_already_exists(self, driver_per_project, mock_arca_client, mock_manila_share):
        """Test idempotency for per_project strategy."""
        # SVM exists
        mock_arca_client.get_svm.return_value = {
            "name": "manila_test-project-id",
            "vip": "192.168.100.10",
        }

        # Share already exists
        mock_arca_client.create_volume.side_effect = arca_exceptions.ArcaShareAlreadyExists(
            share_id="share-share-123"
        )

        mock_arca_client.get_volume.return_value = {
            "name": "share-share-123",
            "export_path": "192.168.100.10:/exports/manila_test-project-id/share-share-123",
        }

        exports = driver_per_project.create_share(Mock(), mock_manila_share, None)

        assert exports[0]["path"].endswith("/share-share-123")
        # Should not have created SVM (it existed)
        mock_arca_client.create_svm.assert_not_called()

    @pytest.fixture
    def driver_manual(self, mock_manila_driver_config, mock_arca_client):
        """Create driver with manual strategy."""
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "manual"
            drv.do_setup(Mock())
            return drv

    def test_create_share_manual_already_exists(self, driver_manual, mock_arca_client, mock_manila_share):
        """Test idempotency for manual strategy."""
        mock_manila_share["share_type"]["extra_specs"] = {"arca_manila:svm_name": "target-svm"}

        mock_arca_client.create_volume.side_effect = arca_exceptions.ArcaShareAlreadyExists(
            share_id="share-share-123"
        )

        mock_arca_client.get_volume.return_value = {
            "name": "share-share-123",
            "export_path": "192.168.100.5:/exports/target-svm/share-share-123",
        }

        exports = driver_manual.create_share(Mock(), mock_manila_share, None)

        assert exports[0]["path"] == "192.168.100.5:/exports/target-svm/share-share-123"


class TestDeleteOperationIdempotency:
    """Test idempotent delete operations."""

    @pytest.fixture
    def driver(self, mock_manila_driver_config, mock_arca_client):
        """Create driver with shared strategy."""
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "shared"
            drv.configuration.arca_storage_default_svm = "test-svm"
            drv.do_setup(Mock())
            return drv

    def test_delete_nonexistent_share_succeeds(self, driver, mock_arca_client, mock_manila_share):
        """Test that deleting non-existent share is idempotent (succeeds)."""
        mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"

        mock_arca_client.delete_volume.side_effect = arca_exceptions.ArcaShareNotFound(
            share_id="share-share-123"
        )

        # Should not raise exception - delete is idempotent
        driver.delete_share(Mock(), mock_manila_share, None)

        mock_arca_client.delete_volume.assert_called_once()

    def test_delete_nonexistent_snapshot_succeeds(self, driver, mock_arca_client, mock_manila_snapshot):
        """Test that deleting non-existent snapshot is idempotent."""
        mock_manila_snapshot["share"]["metadata"] = {"arca_svm_name": "test-svm"}

        mock_arca_client.delete_snapshot.side_effect = arca_exceptions.ArcaSnapshotNotFound(
            snapshot_id="snapshot-snapshot-123"
        )

        # Should not raise exception
        driver.delete_snapshot(Mock(), mock_manila_snapshot, None)

        mock_arca_client.delete_snapshot.assert_called_once()

    def test_delete_share_api_error_raises(self, driver, mock_arca_client, mock_manila_share):
        """Test that non-404 errors during delete are propagated."""
        mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"

        mock_arca_client.delete_volume.side_effect = arca_exceptions.ArcaManilaAPIError(
            "Internal server error"
        )

        # Non-404 errors should be raised
        with pytest.raises(arca_exceptions.ArcaManilaAPIError):
            driver.delete_share(Mock(), mock_manila_share, None)


class TestSnapshotOperationIdempotency:
    """Test idempotent snapshot operations."""

    @pytest.fixture
    def driver(self, mock_manila_driver_config, mock_arca_client):
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "shared"
            drv.configuration.arca_storage_default_svm = "test-svm"
            drv.do_setup(Mock())
            return drv

    def test_create_snapshot_missing_share_metadata_for_shared(self, driver, mock_manila_snapshot):
        """Test snapshot creation when share is missing (but strategy is shared)."""
        # For shared strategy, we can infer SVM from default
        mock_manila_snapshot["share"]["metadata"] = {}  # Empty metadata

        # Should succeed using default SVM
        driver.create_snapshot(Mock(), mock_manila_snapshot, None)

    def test_delete_snapshot_with_snapshot_metadata_fallback(self, driver, mock_arca_client):
        """Test snapshot deletion uses snapshot metadata when share unavailable."""
        # Snapshot has its own SVM metadata
        snapshot = {
            "id": "snapshot-123",
            "share_id": "share-123",
            "metadata": {"arca_svm_name": "test-svm"},
        }

        driver.delete_snapshot(Mock(), snapshot, None)

        # Should use SVM from snapshot metadata
        mock_arca_client.delete_snapshot.assert_called_once_with(
            name="snapshot-snapshot-123",
            svm="test-svm",
            volume="share-share-123",
        )


class TestMetadataPersistence:
    """Test that SVM metadata is properly persisted."""

    @pytest.fixture
    def driver(self, mock_manila_driver_config, mock_arca_client):
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "shared"
            drv.configuration.arca_storage_default_svm = "test-svm"
            drv.do_setup(Mock())
            return drv

    def test_create_share_persists_svm_metadata(self, driver, mock_arca_client, mock_manila_share):
        """Test that SVM name is persisted in share metadata."""
        mock_arca_client.create_volume.return_value = {
            "name": "share-share-123",
            "export_path": "192.168.100.5:/exports/test-svm/share-share-123",
        }

        driver.create_share(Mock(), mock_manila_share, None)

        # Verify metadata was set
        assert mock_manila_share["metadata"]["arca_svm_name"] == "test-svm"

    def test_create_snapshot_persists_svm_metadata(self, driver, mock_arca_client, mock_manila_snapshot):
        """Test that SVM name is persisted in snapshot metadata."""
        mock_arca_client.create_snapshot.return_value = {"name": "snapshot-snapshot-123"}

        driver.create_snapshot(Mock(), mock_manila_snapshot, None)

        # Verify metadata was set on snapshot
        assert mock_manila_snapshot["metadata"]["arca_svm_name"] == "test-svm"

    def test_create_share_from_snapshot_preserves_svm(self, driver, mock_arca_client, mock_manila_snapshot):
        """Test that SVM is preserved when cloning from snapshot."""
        mock_manila_snapshot["share"]["metadata"] = {"arca_svm_name": "test-svm"}

        new_share = {
            "id": "share-456",
            "size": 10,
            "project_id": "test-project-id",
            "metadata": {},
        }

        driver.create_share_from_snapshot(Mock(), new_share, mock_manila_snapshot, None)

        # New share should have same SVM as source
        assert new_share["metadata"]["arca_svm_name"] == "test-svm"

    def test_operations_use_persisted_metadata(self, driver, mock_arca_client, mock_manila_share):
        """Test that subsequent operations use persisted metadata."""
        mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"

        # All these operations should use the persisted SVM name
        driver.extend_share(mock_manila_share, 20, None)
        assert mock_arca_client.resize_volume.call_args[1]["svm"] == "test-svm"

        driver.delete_share(Mock(), mock_manila_share, None)
        assert mock_arca_client.delete_volume.call_args[1]["svm"] == "test-svm"
