"""Unit tests for Manila driver QoS handling."""

from unittest.mock import Mock, patch

import pytest

from arca_storage.openstack.manila import driver as manila_driver
from arca_storage.openstack.manila import exceptions as arca_exceptions


@pytest.fixture
def driver(mock_manila_driver_config, mock_arca_client):
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


def test_create_share_applies_qos_specs(driver, mock_arca_client, mock_manila_share):
    mock_manila_share["share_type"]["extra_specs"] = {
        "arca_manila:read_iops_sec": "1000",
        "arca_manila:write_iops_sec": "500",
        "arca_manila:read_bytes_sec": "1048576",
    }

    driver.create_share(Mock(), mock_manila_share, None)

    mock_arca_client.apply_qos.assert_called_once_with(
        volume="share-share-123",
        svm="test-svm",
        read_iops=1000,
        write_iops=500,
        read_bps=1048576,
        write_bps=None,
    )


def test_create_share_fails_when_qos_application_fails(
    driver, mock_arca_client, mock_manila_share
):
    mock_manila_share["share_type"]["extra_specs"] = {
        "arca_manila:read_iops_sec": "1000",
    }
    mock_arca_client.apply_qos.side_effect = RuntimeError("qos backend unavailable")

    with pytest.raises(
        manila_driver.manila_exception.ShareBackendException,
        match="Failed to create share: Failed to apply QoS",
    ):
        driver.create_share(Mock(), mock_manila_share, None)

    mock_arca_client.apply_qos.assert_called_once()


def test_qos_failure_logs_redact_identifiers_and_backend_errors(
    driver, mock_arca_client, mock_manila_share
):
    mock_manila_share["id"] = "share-secret-token"
    mock_manila_share["metadata"]["arca_svm_name"] = "test-svm"
    mock_manila_share["share_type"]["extra_specs"] = {
        "arca_manila:read_iops_sec": "1000",
    }
    mock_arca_client.apply_qos.side_effect = RuntimeError(
        "Authorization: Bearer secret-token password=hunter2"
    )

    with patch.object(manila_driver.LOG, "error") as mock_error:
        with pytest.raises(manila_driver.manila_exception.ShareBackendException):
            driver.create_share(Mock(), mock_manila_share, None)

    rendered_calls = " ".join(str(call.args) for call in mock_error.call_args_list)
    assert "share-secret-token" not in rendered_calls
    assert "test-svm" not in rendered_calls
    assert "secret-token" not in rendered_calls
    assert "hunter2" not in rendered_calls
    assert "Failed to apply QoS to share" in rendered_calls


def test_create_share_fails_on_invalid_qos_specs(
    driver, mock_arca_client, mock_manila_share
):
    mock_manila_share["share_type"]["extra_specs"] = {
        "arca_manila:read_iops_sec": "not-an-integer",
    }

    with pytest.raises(
        manila_driver.manila_exception.ShareBackendException,
        match="Failed to create share: Invalid QoS extra specs",
    ):
        driver.create_share(Mock(), mock_manila_share, None)

    mock_arca_client.create_volume.assert_not_called()
    mock_arca_client.apply_qos.assert_not_called()


@pytest.mark.parametrize("spec_value", ["0", "-1"])
def test_create_share_fails_on_non_positive_qos_specs(
    driver, mock_arca_client, mock_manila_share, spec_value
):
    mock_manila_share["share_type"]["extra_specs"] = {
        "arca_manila:read_iops_sec": spec_value,
    }

    with pytest.raises(
        manila_driver.manila_exception.ShareBackendException,
        match="Failed to create share: Invalid QoS extra specs.*must be greater than 0",
    ):
        driver.create_share(Mock(), mock_manila_share, None)

    mock_arca_client.create_volume.assert_not_called()
    mock_arca_client.apply_qos.assert_not_called()


def test_create_share_existing_volume_applies_qos_specs(
    driver, mock_arca_client, mock_manila_share
):
    mock_manila_share["share_type"]["extra_specs"] = {
        "arca_manila:write_bytes_sec": "2097152",
    }
    mock_arca_client.create_volume.side_effect = arca_exceptions.ArcaShareAlreadyExists(
        share_id="share-share-123"
    )

    exports = driver.create_share(Mock(), mock_manila_share, None)

    assert exports[0]["path"] == "192.168.100.5:/exports/test-svm/share-share-123"
    mock_arca_client.apply_qos.assert_called_once_with(
        volume="share-share-123",
        svm="test-svm",
        read_iops=None,
        write_iops=None,
        read_bps=None,
        write_bps=2097152,
    )


def test_create_share_from_snapshot_fails_when_qos_application_fails(
    driver, mock_arca_client, mock_manila_snapshot
):
    new_share = {
        "id": "share-456",
        "size": 10,
        "project_id": "test-project-id",
        "metadata": {},
        "share_type": {"extra_specs": {"arca_manila:read_iops_sec": "1000"}},
    }
    mock_arca_client.apply_qos.side_effect = RuntimeError("qos backend unavailable")

    with pytest.raises(
        manila_driver.manila_exception.ShareBackendException,
        match="Failed to create share from snapshot: Failed to apply QoS",
    ):
        driver.create_share_from_snapshot(Mock(), new_share, mock_manila_snapshot, None)

    mock_arca_client.apply_qos.assert_called_once()
