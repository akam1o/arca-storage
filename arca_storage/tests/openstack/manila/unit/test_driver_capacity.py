"""Unit tests for Manila capacity aggregation."""

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
