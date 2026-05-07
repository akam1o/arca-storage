"""Unit tests for Manila driver with per_project SVM strategy."""

from unittest.mock import Mock, patch

import pytest

from arca_storage.openstack.manila import driver as manila_driver
from arca_storage.openstack.manila import exceptions as arca_exceptions


class TestArcaStorageManilaDriverPerProjectStrategy:
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
                "192.168.100.0/24|192.168.100.10-192.168.100.10:100"
            ]
            drv.configuration.arca_storage_svm_prefix = "manila_"
            drv.do_setup(Mock())
            return drv

    def test_do_setup_parses_pools(self, driver):
        assert driver._svm_strategy_effective == "per_project"
        assert len(driver._network_allocator._ip_vlan_pools) == 1
        pool = driver._network_allocator._ip_vlan_pools[0]
        assert str(pool["ip_network"]) == "192.168.100.0/24"
        assert pool["vlan_id"] == 100

    def test_create_share_creates_svm_when_missing(self, driver, mock_arca_client, mock_manila_share):
        # Force "SVM not found" for the per-project SVM name, so driver will create it.
        def get_svm_side_effect(name):
            if name == "manila_test-project-id":
                raise arca_exceptions.ArcaSVMNotFound(svm_name=name)
            return {"name": name}

        mock_arca_client.get_svm.side_effect = get_svm_side_effect
        mock_arca_client.list_svms.return_value = []
        mock_arca_client.create_svm.return_value = {
            "name": "manila_test-project-id",
            "vip": "192.168.100.10",
            "ip_cidr": "192.168.100.10/24",
            "vlan_id": 100,
        }
        mock_arca_client.create_volume.return_value = {
            "name": "share-share-123",
            "export_path": "192.168.100.10:/exports/manila_test-project-id/share-share-123",
        }

        exports = driver.create_share(Mock(), mock_manila_share, None)

        assert exports[0]["path"].endswith("/share-share-123")
        assert mock_manila_share["metadata"]["arca_svm_name"] == "manila_test-project-id"

        mock_arca_client.create_svm.assert_called_once_with(
            name="manila_test-project-id",
            vlan_id=100,
            ip_cidr="192.168.100.10/24",
            gateway="192.168.100.1",
            mtu=1500,
            root_volume_size_gib=None,
        )
        mock_arca_client.create_volume.assert_called_once()

    def test_create_share_ignores_user_supplied_svm_metadata(
        self, driver, mock_arca_client, mock_manila_share
    ):
        mock_manila_share["metadata"]["arca_svm_name"] = "manila_other-project"
        mock_arca_client.create_volume.return_value = {
            "name": "share-share-123",
            "export_path": "192.168.100.10:/exports/manila_test-project-id/share-share-123",
        }

        driver.create_share(Mock(), mock_manila_share, None)

        assert mock_manila_share["metadata"]["arca_svm_name"] == "manila_test-project-id"
        mock_arca_client.create_volume.assert_called_once_with(
            name="share-share-123",
            svm="manila_test-project-id",
            size_gib=10,
            thin=True,
            fs_type="xfs",
        )

    def test_extend_share_uses_backend_svm_without_project_id(
        self, driver, mock_arca_client
    ):
        share = {
            "id": "share-123",
            "size": 10,
            "metadata": {"arca_svm_name": "manila_user_supplied"},
        }
        mock_arca_client.list_volumes.return_value = [
            {"name": "share-share-123", "svm": "manila_test-project-id"}
        ]

        driver.extend_share(share, 20, None)

        mock_arca_client.resize_volume.assert_called_once_with(
            name="share-share-123",
            svm="manila_test-project-id",
            new_size_gib=20,
        )

    def test_delete_share_missing_backend_volume_succeeds_without_project_id(
        self, driver, mock_arca_client
    ):
        share = {
            "id": "share-123",
            "size": 10,
            "metadata": {"arca_svm_name": "manila_user_supplied"},
        }
        mock_arca_client.list_volumes.return_value = []

        driver.delete_share(Mock(), share, None)

        mock_arca_client.delete_volume.assert_not_called()

    def test_delete_snapshot_without_share_uses_backend_svm(
        self, driver, mock_arca_client
    ):
        snapshot = {
            "id": "snapshot-123",
            "share_id": "share-123",
            "metadata": {"arca_svm_name": "manila_user_supplied"},
        }
        mock_arca_client.list_volumes.return_value = [
            {"name": "share-share-123", "svm": "manila_test-project-id"}
        ]

        driver.delete_snapshot(Mock(), snapshot, None)

        mock_arca_client.delete_snapshot.assert_called_once_with(
            name="snapshot-snapshot-123",
            svm="manila_test-project-id",
            volume="share-share-123",
        )
