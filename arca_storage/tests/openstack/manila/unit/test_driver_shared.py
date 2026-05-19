"""Unit tests for Manila driver with shared SVM strategy."""

from unittest.mock import Mock, patch

import pytest

from arca_storage.openstack.manila import driver as manila_driver


class TestArcaStorageManilaDriverSharedStrategy:
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

    def test_do_setup_checks_default_svm_exists(self, driver, mock_arca_client):
        mock_arca_client.get_svm.assert_called_with("test-svm")

    def test_do_setup_passes_token_auth_to_api_client(
        self, mock_manila_driver_config, mock_arca_client
    ):
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "shared"
            drv.configuration.arca_storage_default_svm = "test-svm"
            drv.do_setup(Mock())

        assert mock_client_class.call_args.kwargs["auth_type"] == "token"
        assert mock_client_class.call_args.kwargs["api_token"] == "test-token"
        assert mock_client_class.call_args.kwargs["allow_insecure_token_transport"] is True

    def test_do_setup_rejects_token_auth_without_token(
        self, mock_manila_driver_config
    ):
        drv = manila_driver.ArcaStorageManilaDriver()
        drv.configuration = mock_manila_driver_config
        drv.configuration.arca_storage_api_token = None

        with pytest.raises(
            manila_driver.manila_exception.ManilaException,
            match="arca_storage_api_token",
        ):
            drv.do_setup(Mock())

    def test_do_setup_rejects_client_key_without_client_cert(
        self, mock_manila_driver_config, mock_arca_client
    ):
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_api_client_key = "/etc/arca/client.key"

            with pytest.raises(
                manila_driver.manila_exception.ManilaException,
                match="arca_storage_api_client_key",
            ):
                drv.do_setup(Mock())

    def test_do_setup_rejects_remote_http_token_without_opt_in(
        self, mock_manila_driver_config
    ):
        drv = manila_driver.ArcaStorageManilaDriver()
        drv.configuration = mock_manila_driver_config
        drv.configuration.arca_storage_allow_insecure_api_token_transport = False

        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            with pytest.raises(
                manila_driver.manila_exception.ManilaException,
                match="remote plain HTTP",
            ):
                drv.do_setup(Mock())

        mock_client_class.assert_not_called()

    def test_update_share_stats_includes_pool_capabilities(self, driver):
        stats = driver._update_share_stats()
        assert stats["storage_protocol"] == "NFS"
        assert len(stats["pools"]) == 1
        pool = stats["pools"][0]
        assert pool["pool_name"] == "test-svm"
        assert "snapshot_support" in pool
        assert "create_share_from_snapshot_support" in pool

    def test_create_share_persists_svm_metadata(self, driver, mock_arca_client, mock_manila_share):
        mock_arca_client.create_volume.return_value = {
            "name": "share-share-123",
            "export_path": "192.168.100.5:/exports/test-svm/share-share-123",
        }

        exports = driver.create_share(Mock(), mock_manila_share, None)
        assert exports == [
            {"path": "192.168.100.5:/exports/test-svm/share-share-123", "is_admin_only": False, "metadata": {}}
        ]
        assert mock_manila_share["metadata"]["arca_svm_name"] == "test-svm"
        mock_arca_client.create_volume.assert_called_once_with(
            name="share-share-123",
            svm="test-svm",
            size_gib=10,
            thin=True,
            fs_type="xfs",
        )

    def test_create_share_rejects_unsafe_backend_export_path(
        self, driver, mock_arca_client, mock_manila_share
    ):
        mock_arca_client.create_volume.return_value = {
            "name": "share-share-123",
            "export_path": "192.168.100.5:/exports/../secret/share-share-123",
        }

        with pytest.raises(
            manila_driver.manila_exception.ShareBackendException,
            match="export_path",
        ):
            driver.create_share(Mock(), mock_manila_share, None)

        mock_arca_client.apply_qos.assert_not_called()

    def test_create_share_ignores_user_supplied_svm_metadata(
        self, driver, mock_arca_client, mock_manila_share
    ):
        mock_manila_share["metadata"]["arca_svm_name"] = "other-svm"
        mock_arca_client.create_volume.return_value = {
            "name": "share-share-123",
            "export_path": "192.168.100.5:/exports/test-svm/share-share-123",
        }

        driver.create_share(Mock(), mock_manila_share, None)

        assert mock_manila_share["metadata"]["arca_svm_name"] == "test-svm"
        mock_arca_client.create_volume.assert_called_once_with(
            name="share-share-123",
            svm="test-svm",
            size_gib=10,
            thin=True,
            fs_type="xfs",
        )

    def test_delete_share_calls_delete_volume(self, driver, mock_arca_client, mock_manila_share):
        mock_manila_share["metadata"]["arca_svm_name"] = "user-supplied-svm"
        mock_manila_share["export_locations"] = [
            {"path": "192.168.100.5:/exports/test-svm/share-share-123"}
        ]
        driver.delete_share(Mock(), mock_manila_share, None)
        mock_arca_client.delete_volume.assert_called_once_with(
            name="share-share-123",
            svm="test-svm",
            force=False,
        )

    def test_delete_share_rejects_unsafe_export_location(
        self, driver, mock_arca_client, mock_manila_share
    ):
        mock_manila_share["metadata"]["arca_svm_name"] = "user-supplied-svm"
        mock_manila_share["export_locations"] = [
            {"path": "192.168.100.5:/exports/../secret/share-share-123"}
        ]

        with pytest.raises(
            manila_driver.manila_exception.ShareBackendException,
            match="export_path",
        ):
            driver.delete_share(Mock(), mock_manila_share, None)

        mock_arca_client.delete_volume.assert_not_called()

    def test_extend_share_calls_resize_volume(self, driver, mock_arca_client, mock_manila_share):
        mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"
        driver.extend_share(mock_manila_share, 20, None)
        mock_arca_client.resize_volume.assert_called_once_with(
            name="share-share-123",
            svm="test-svm",
            new_size_gib=20,
        )

    def test_create_snapshot_persists_svm_metadata(self, driver, mock_arca_client, mock_manila_snapshot):
        mock_arca_client.create_snapshot.return_value = {"name": "snapshot-snapshot-123"}
        driver.create_snapshot(Mock(), mock_manila_snapshot, None)
        assert mock_manila_snapshot["metadata"]["arca_svm_name"] == "test-svm"
        mock_arca_client.create_snapshot.assert_called_once_with(
            name="snapshot-snapshot-123",
            svm="test-svm",
            volume="share-share-123",
        )

    def test_create_snapshot_rejects_unsafe_backend_export_path(
        self, driver, mock_arca_client, mock_manila_snapshot
    ):
        mock_arca_client.create_snapshot.return_value = {
            "name": "snapshot-snapshot-123",
            "export_path": "192.168.100.5:/exports/../secret/snapshot-snapshot-123",
        }

        with pytest.raises(
            manila_driver.manila_exception.ShareBackendException,
            match="export_path",
        ):
            driver.create_snapshot(Mock(), mock_manila_snapshot, None)

    def test_delete_snapshot_uses_shared_svm_when_share_missing(self, driver, mock_arca_client):
        snapshot = {"id": "snapshot-123", "share_id": "share-123", "metadata": {"arca_svm_name": "test-svm"}}
        driver.delete_snapshot(Mock(), snapshot, None)
        mock_arca_client.delete_snapshot.assert_called_once_with(
            name="snapshot-snapshot-123",
            svm="test-svm",
            volume="share-share-123",
        )

    def test_delete_snapshot_without_share_uses_backend_svm(self, driver, mock_arca_client):
        driver.configuration.arca_storage_default_svm = "target-svm"
        mock_arca_client.list_volumes.return_value = [
            {"name": "share-share-123", "svm": "source-svm"},
        ]
        snapshot = {"id": "snapshot-123", "share_id": "share-123", "metadata": {"arca_svm_name": "target-svm"}}

        driver.delete_snapshot(Mock(), snapshot, None)

        mock_arca_client.delete_snapshot.assert_called_once_with(
            name="snapshot-snapshot-123",
            svm="source-svm",
            volume="share-share-123",
        )

    def test_create_share_from_snapshot_without_share_uses_backend_svm(self, driver, mock_arca_client):
        driver.configuration.arca_storage_default_svm = "target-svm"
        mock_arca_client.list_volumes.return_value = [
            {"name": "share-share-123", "svm": "source-svm"},
        ]
        snapshot = {"id": "snapshot-123", "share_id": "share-123", "metadata": {"arca_svm_name": "target-svm"}}
        new_share = {"id": "share-456", "size": 10, "project_id": "test-project-id", "metadata": {}}

        driver.create_share_from_snapshot(Mock(), new_share, snapshot, None)

        mock_arca_client.clone_volume_from_snapshot.assert_called_once_with(
            name="share-share-456",
            svm="source-svm",
            source_volume="share-share-123",
            snapshot_name="snapshot-snapshot-123",
            size_gib=10,
        )
        assert new_share["metadata"]["arca_svm_name"] == "source-svm"

    def test_create_share_from_snapshot_persists_svm_metadata(self, driver, mock_arca_client, mock_manila_snapshot):
        new_share = {"id": "share-456", "size": 10, "project_id": "test-project-id", "metadata": {}}
        driver.create_share_from_snapshot(Mock(), new_share, mock_manila_snapshot, None)
        assert new_share["metadata"]["arca_svm_name"] == "test-svm"
        mock_arca_client.clone_volume_from_snapshot.assert_called_once_with(
            name="share-share-456",
            svm="test-svm",
            source_volume="share-share-123",
            snapshot_name="snapshot-snapshot-123",
            size_gib=10,
        )

    def test_create_share_from_snapshot_rejects_unsafe_backend_export_path(
        self, driver, mock_arca_client, mock_manila_snapshot
    ):
        new_share = {"id": "share-456", "size": 10, "project_id": "test-project-id", "metadata": {}}
        mock_arca_client.clone_volume_from_snapshot.return_value = {
            "name": "share-share-456",
            "export_path": "192.168.100.5:/exports/../secret/share-share-456",
        }

        with pytest.raises(
            manila_driver.manila_exception.ShareBackendException,
            match="export_path",
        ):
            driver.create_share_from_snapshot(Mock(), new_share, mock_manila_snapshot, None)

        mock_arca_client.apply_qos.assert_not_called()

    def test_update_access_add_rule(self, driver, mock_arca_client, mock_manila_share, mock_access_rules):
        mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"
        driver.update_access(Mock(), mock_manila_share, [], add_rules=mock_access_rules, delete_rules=[], share_server=None)
        mock_arca_client.create_export.assert_called_once_with(
            svm="test-svm",
            volume="share-share-123",
            client="192.168.1.100/32",
            access="rw",
            root_squash=True,
        )

    def test_update_access_reports_add_rule_failures(self, driver, mock_arca_client, mock_manila_share, mock_access_rules):
        mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"
        mock_arca_client.create_export.side_effect = RuntimeError("backend unavailable")

        with pytest.raises(manila_driver.manila_exception.ShareBackendException, match="Failed to update 1 access"):
            driver.update_access(Mock(), mock_manila_share, [], add_rules=mock_access_rules, delete_rules=[], share_server=None)

        mock_arca_client.create_export.assert_called_once()

    def test_update_access_reports_delete_rule_failures(self, driver, mock_arca_client, mock_manila_share, mock_access_rules):
        mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"
        mock_arca_client.delete_export.side_effect = RuntimeError("backend unavailable")

        with pytest.raises(manila_driver.manila_exception.ShareBackendException, match="Failed to update 1 access"):
            driver.update_access(Mock(), mock_manila_share, [], add_rules=[], delete_rules=mock_access_rules, share_server=None)

        mock_arca_client.delete_export.assert_called_once()

    def test_update_access_delete_rule_normalizes_bare_ip(self, driver, mock_arca_client, mock_manila_share, mock_access_rules):
        mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"

        driver.update_access(Mock(), mock_manila_share, [], add_rules=[], delete_rules=mock_access_rules, share_server=None)

        mock_arca_client.delete_export.assert_called_once_with(
            svm="test-svm",
            volume="share-share-123",
            client="192.168.1.100/32",
        )

    def test_update_access_reconcile_fallback(self, driver, mock_arca_client, mock_manila_share):
        mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"
        mock_arca_client.list_exports.return_value = [{"client": "10.0.0.0/24", "access": "rw"}]
        desired = [{"access_type": "ip", "access_to": "192.168.1.100", "access_level": "ro"}]

        driver.update_access(Mock(), mock_manila_share, desired, add_rules=[], delete_rules=[], share_server=None)

        mock_arca_client.delete_export.assert_called_once_with(
            svm="test-svm",
            volume="share-share-123",
            client="10.0.0.0/24",
        )
        mock_arca_client.create_export.assert_called_once_with(
            svm="test-svm",
            volume="share-share-123",
            client="192.168.1.100/32",
            access="ro",
            root_squash=True,
        )

    def test_update_access_reconcile_fallback_removes_all_exports(self, driver, mock_arca_client, mock_manila_share):
        mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"
        mock_arca_client.list_exports.return_value = [{"client": "10.0.0.0/24", "access": "rw"}]

        driver.update_access(Mock(), mock_manila_share, [], add_rules=[], delete_rules=[], share_server=None)

        mock_arca_client.delete_export.assert_called_once_with(
            svm="test-svm",
            volume="share-share-123",
            client="10.0.0.0/24",
        )
        mock_arca_client.create_export.assert_not_called()

    def test_update_access_reconcile_fallback_reports_delete_failures(self, driver, mock_arca_client, mock_manila_share):
        mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"
        mock_arca_client.list_exports.return_value = [{"client": "10.0.0.0/24", "access": "rw"}]
        mock_arca_client.delete_export.side_effect = RuntimeError("backend unavailable")
        desired = [{"access_type": "ip", "access_to": "192.168.1.100", "access_level": "ro"}]

        with pytest.raises(manila_driver.manila_exception.ShareBackendException, match="Failed to reconcile 1 access"):
            driver.update_access(Mock(), mock_manila_share, desired, add_rules=[], delete_rules=[], share_server=None)

        mock_arca_client.delete_export.assert_called_once()

    def test_update_access_reconcile_fallback_reports_add_failures(self, driver, mock_arca_client, mock_manila_share):
        mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"
        mock_arca_client.list_exports.return_value = []
        mock_arca_client.create_export.side_effect = RuntimeError("backend unavailable")
        desired = [{"access_type": "ip", "access_to": "192.168.1.100", "access_level": "ro"}]

        with pytest.raises(manila_driver.manila_exception.ShareBackendException, match="Failed to reconcile 1 access"):
            driver.update_access(Mock(), mock_manila_share, desired, add_rules=[], delete_rules=[], share_server=None)

        mock_arca_client.create_export.assert_called_once()

    def test_update_access_rejects_unsupported_access_type(self, driver, mock_manila_share):
        mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"
        bad = [{"access_type": "user", "access_to": "alice"}]
        with pytest.raises(manila_driver.manila_exception.InvalidShareAccess):
            driver.update_access(Mock(), mock_manila_share, bad, add_rules=[], delete_rules=[], share_server=None)
