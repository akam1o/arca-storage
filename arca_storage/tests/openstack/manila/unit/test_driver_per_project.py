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
        assert len(driver._ip_vlan_pools) == 1
        pool = driver._ip_vlan_pools[0]
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

