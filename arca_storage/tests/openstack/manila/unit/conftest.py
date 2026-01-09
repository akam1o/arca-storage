"""Pytest configuration and fixtures for Manila unit tests."""

from unittest.mock import Mock

import pytest


@pytest.fixture
def mock_manila_driver_config():
    """Create a mock oslo.config-like configuration object for the driver."""
    config = Mock()

    # Base driver / scheduler fields
    config.share_backend_name = "arca_test_backend"

    # API configuration (required by do_setup)
    config.arca_storage_use_api = True
    config.arca_storage_api_endpoint = "http://192.168.10.5:8080"
    config.arca_storage_api_timeout = 30
    config.arca_storage_api_retry_count = 3
    config.arca_storage_verify_ssl = False

    # API auth (optional)
    config.arca_storage_api_auth_type = "none"
    config.arca_storage_api_token = None
    config.arca_storage_api_username = None
    config.arca_storage_api_password = None
    config.arca_storage_api_ca_bundle = None
    config.arca_storage_api_client_cert = None
    config.arca_storage_api_client_key = None

    # Multi-tenancy strategy
    config.arca_storage_svm_strategy = "shared"
    config.arca_storage_default_svm = "test-svm"
    config.arca_storage_svm_prefix = "manila_"

    # per_project network allocation
    config.arca_storage_per_project_ip_pools = []
    config.arca_storage_per_project_mtu = 1500
    config.arca_storage_per_project_root_volume_size_gib = None

    # Scheduler capacity reporting
    config.arca_storage_max_over_subscription_ratio = 20.0
    config.arca_storage_reserved_percentage = 0
    config.arca_storage_reserved_share_percentage = 0
    config.arca_storage_reserved_share_from_snapshot_percentage = 0

    # Feature flags
    config.arca_storage_snapshot_support = True
    config.arca_storage_create_share_from_snapshot_support = True
    config.arca_storage_revert_to_snapshot_support = False
    config.arca_storage_mount_snapshot_support = False

    return config


@pytest.fixture
def mock_arca_client():
    """Create a mock ARCA Storage Manila API client."""
    client = Mock()

    # SVM operations
    client.list_svms.return_value = [{"name": "test-svm", "vip": "192.168.100.5", "vlan_id": 100}]
    client.get_svm.return_value = {
        "name": "test-svm",
        "vip": "192.168.100.5",
        "ip_cidr": "192.168.100.5/24",
        "vlan_id": 100,
    }
    client.get_svm_capacity.return_value = {
        "total_gb": 1000,
        "free_gb": 800,
        "provisioned_gb": 200,
    }
    client.create_svm.return_value = {
        "name": "manila_test-project-id",
        "vip": "192.168.100.10",
        "ip_cidr": "192.168.100.10/24",
        "vlan_id": 100,
    }

    # Volume (share) operations
    client.create_volume.return_value = {"name": "share-share-123", "export_path": "192.168.100.5:/exports/test-svm/share-share-123"}
    client.get_volume.return_value = {"name": "share-share-123", "export_path": "192.168.100.5:/exports/test-svm/share-share-123"}
    client.delete_volume.return_value = None
    client.resize_volume.return_value = {"name": "share-share-123"}
    client.clone_volume_from_snapshot.return_value = {
        "name": "share-share-456",
        "export_path": "192.168.100.5:/exports/test-svm/share-share-456",
    }

    # Snapshot operations
    client.create_snapshot.return_value = {"name": "snapshot-snapshot-123"}
    client.delete_snapshot.return_value = None
    client.list_snapshots.return_value = []

    # Export / access rules
    client.create_export.return_value = {"client": "192.168.1.100/32", "access": "rw"}
    client.delete_export.return_value = None
    client.list_exports.return_value = []

    # QoS
    client.apply_qos.return_value = {}

    return client


@pytest.fixture
def mock_share_type():
    return {"extra_specs": {}}


@pytest.fixture
def mock_manila_share(mock_share_type):
    """Create a dict-like Manila share object as the driver expects."""
    return {
        "id": "share-123",
        "size": 10,
        "project_id": "test-project-id",
        "share_type": mock_share_type,
        "metadata": {},
    }


@pytest.fixture
def mock_manila_snapshot(mock_manila_share):
    """Create a dict-like Manila snapshot object as the driver expects."""
    return {
        "id": "snapshot-123",
        "share_id": "share-123",
        "share": mock_manila_share,
        "metadata": {},
    }


@pytest.fixture
def mock_access_rules():
    return [
        {
            "id": "rule-123",
            "access_type": "ip",
            "access_to": "192.168.1.100",
            "access_level": "rw",
        }
    ]
