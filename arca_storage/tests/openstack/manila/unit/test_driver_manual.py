"""Unit tests for Manila driver with manual SVM strategy."""

from unittest.mock import Mock, patch

import pytest

from arca_storage.openstack.manila import driver as manila_driver
from arca_storage.openstack.manila import exceptions as arca_exceptions


class TestArcaStorageManilaDriverManualStrategy:
    @pytest.fixture
    def driver(self, mock_manila_driver_config, mock_arca_client):
        with patch(
            "arca_storage.openstack.manila.driver.arca_client.ArcaManilaClient"
        ) as mock_client_class:
            mock_client_class.return_value = mock_arca_client

            drv = manila_driver.ArcaStorageManilaDriver()
            drv.configuration = mock_manila_driver_config
            drv.configuration.arca_storage_svm_strategy = "manual"
            drv.do_setup(Mock())
            return drv

    def test_create_share_uses_share_type_extra_specs(self, driver, mock_arca_client, mock_manila_share):
        mock_manila_share["share_type"]["extra_specs"] = {"arca_manila:svm_name": "target-svm"}
        mock_arca_client.create_volume.return_value = {
            "name": "share-share-123",
            "export_path": "192.168.100.5:/exports/target-svm/share-share-123",
        }

        exports = driver.create_share(Mock(), mock_manila_share, None)
        assert exports[0]["path"] == "192.168.100.5:/exports/target-svm/share-share-123"
        assert mock_manila_share["metadata"]["arca_svm_name"] == "target-svm"
        mock_arca_client.create_volume.assert_called_once_with(
            name="share-share-123",
            svm="target-svm",
            size_gib=10,
            thin=True,
            fs_type="xfs",
        )

    def test_create_share_missing_svm_name_fails(self, driver, mock_manila_share):
        mock_manila_share["share_type"]["extra_specs"] = {}
        with pytest.raises(manila_driver.manila_exception.ShareBackendException):
            driver.create_share(Mock(), mock_manila_share, None)

    def test_create_share_whitespace_svm_name_fails(self, driver, mock_manila_share):
        mock_manila_share["share_type"]["extra_specs"] = {"arca_manila:svm_name": "   "}
        with pytest.raises(manila_driver.manila_exception.ShareBackendException):
            driver.create_share(Mock(), mock_manila_share, None)

    def test_create_share_svm_not_found_maps_to_backend_exception(self, driver, mock_arca_client, mock_manila_share):
        mock_manila_share["share_type"]["extra_specs"] = {"arca_manila:svm_name": "nonexistent-svm"}
        mock_arca_client.create_volume.side_effect = arca_exceptions.ArcaSVMNotFound(svm_name="nonexistent-svm")
        with pytest.raises(manila_driver.manila_exception.ShareBackendException):
            driver.create_share(Mock(), mock_manila_share, None)
