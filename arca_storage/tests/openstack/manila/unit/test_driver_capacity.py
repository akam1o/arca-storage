"""Unit tests for Manila capacity aggregation."""

import logging
from unittest.mock import Mock

from arca_storage.openstack.manila import driver as manila_driver


def _driver(mock_manila_driver_config, mock_arca_client):
    drv = manila_driver.ArcaStorageManilaDriver()
    drv.configuration = mock_manila_driver_config
    drv.arca_client = mock_arca_client
    return drv


def test_per_project_capacity_deduplicates_shared_vg(mock_manila_driver_config, mock_arca_client):
    drv = _driver(mock_manila_driver_config, mock_arca_client)
    mock_arca_client.list_svms.return_value = [
        {"name": "manila_project-a"},
        {"name": "manila_project-b"},
        {"name": "other-svm"},
    ]
    capacities = {
        "manila_project-a": {"vg": "vg_pool_01", "total_gb": 1000, "free_gb": 700, "provisioned_gb": 100},
        "manila_project-b": {"vg": "vg_pool_01", "total_gb": 1000, "free_gb": 700, "provisioned_gb": 200},
    }
    mock_arca_client.get_svm_capacity.side_effect = lambda name: capacities[name]

    pool = drv._get_per_project_aggregate_pool_stats()

    assert pool["total_capacity_gb"] == 1000.0
    assert pool["free_capacity_gb"] == 700.0
    assert pool["provisioned_capacity_gb"] == 300.0


def test_manual_capacity_deduplicates_shared_vg_and_sums_unique_vgs(
    mock_manila_driver_config, mock_arca_client
):
    drv = _driver(mock_manila_driver_config, mock_arca_client)
    mock_arca_client.list_svms.return_value = [
        {"name": "svm-a"},
        {"name": "svm-b"},
        {"name": "svm-c"},
    ]
    capacities = {
        "svm-a": {"vg": "vg_pool_01", "total_gb": 1000, "free_gb": 700, "provisioned_gb": 100},
        "svm-b": {"vg": "vg_pool_01", "total_gb": 1000, "free_gb": 700, "provisioned_gb": 200},
        "svm-c": {"vg": "vg_pool_02", "total_gb": 500, "free_gb": 300, "provisioned_gb": 50},
    }
    mock_arca_client.get_svm_capacity.side_effect = lambda name: capacities[name]

    pool = drv._get_manual_aggregate_pool_stats()

    assert pool["total_capacity_gb"] == 1500.0
    assert pool["free_capacity_gb"] == 1000.0
    assert pool["provisioned_capacity_gb"] == 350.0


def test_update_share_stats_redacts_sensitive_pool_errors(
    mock_manila_driver_config, mock_arca_client, caplog
):
    drv = _driver(mock_manila_driver_config, mock_arca_client)
    drv._get_pool_stats = Mock(
        side_effect=RuntimeError("Authorization: Bearer secret-token password=hunter2")
    )
    caplog.set_level(logging.WARNING)

    stats = drv._update_share_stats()

    assert stats["pools"][0]["pool_name"] == "unknown"
    assert "secret-token" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "<redacted>" in caplog.text


def test_get_pool_stats_redacts_sensitive_capacity_errors(
    mock_manila_driver_config, mock_arca_client, caplog
):
    drv = _driver(mock_manila_driver_config, mock_arca_client)
    mock_arca_client.get_svm_capacity.side_effect = RuntimeError(
        "Authorization: Bearer secret-token password=hunter2"
    )
    caplog.set_level(logging.WARNING)

    pool = drv._get_pool_stats("test-svm")

    assert pool["total_capacity_gb"] == "unknown"
    assert pool["free_capacity_gb"] == "unknown"
    assert "secret-token" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "<redacted>" in caplog.text


def test_aggregate_capacity_redacts_sensitive_capacity_errors(
    mock_manila_driver_config, mock_arca_client, caplog
):
    drv = _driver(mock_manila_driver_config, mock_arca_client)
    mock_arca_client.list_svms.return_value = [{"name": "manila_project-a"}]
    mock_arca_client.get_svm_capacity.side_effect = RuntimeError(
        "Authorization: Bearer secret-token password=hunter2"
    )
    caplog.set_level(logging.WARNING)

    pool = drv._get_per_project_aggregate_pool_stats()

    assert pool["total_capacity_gb"] == "unknown"
    assert pool["free_capacity_gb"] == "unknown"
    assert "secret-token" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "<redacted>" in caplog.text


def test_per_project_stats_redacts_sensitive_list_errors(
    mock_manila_driver_config, mock_arca_client, caplog
):
    drv = _driver(mock_manila_driver_config, mock_arca_client)
    mock_arca_client.list_svms.side_effect = RuntimeError(
        "Authorization: Bearer secret-token password=hunter2"
    )
    caplog.set_level(logging.WARNING)

    pool = drv._get_per_project_aggregate_pool_stats()

    assert pool["total_capacity_gb"] == "unknown"
    assert pool["free_capacity_gb"] == "unknown"
    assert "secret-token" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "<redacted>" in caplog.text


def test_manual_stats_redacts_sensitive_list_errors(
    mock_manila_driver_config, mock_arca_client, caplog
):
    drv = _driver(mock_manila_driver_config, mock_arca_client)
    mock_arca_client.list_svms.side_effect = RuntimeError(
        "Authorization: Bearer secret-token password=hunter2"
    )
    caplog.set_level(logging.WARNING)

    pool = drv._get_manual_aggregate_pool_stats()

    assert pool["total_capacity_gb"] == "unknown"
    assert pool["free_capacity_gb"] == "unknown"
    assert "secret-token" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "<redacted>" in caplog.text
