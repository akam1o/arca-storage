"""Unit tests for ARCA Storage API client."""

import unittest
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests

from arca_storage.openstack.cinder import client as arca_client
from arca_storage.openstack.cinder import exceptions as arca_exceptions


class TestArcaStorageClient(unittest.TestCase):
    """Test ArcaStorageClient class."""

    def setUp(self):
        """Set up test fixtures."""
        self.api_endpoint = "http://192.168.10.5:8080"
        self.timeout = 30
        self.retry_count = 3
        self.verify_ssl = False

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_init_success(self, mock_requests):
        """Test successful client initialization."""
        client = arca_client.ArcaStorageClient(
            api_endpoint=self.api_endpoint,
            timeout=self.timeout,
            retry_count=self.retry_count,
            verify_ssl=self.verify_ssl,
        )

        assert client.base_url == self.api_endpoint
        assert client.timeout == self.timeout
        assert client.retry_count == self.retry_count
        assert client.verify_ssl == self.verify_ssl
        assert client.session is not None

    @patch("arca_storage.openstack.cinder.client.requests", None)
    def test_init_without_requests_library(self):
        """Test client initialization fails without requests library."""
        with pytest.raises(ImportError, match="requests library is required"):
            arca_client.ArcaStorageClient(
                api_endpoint=self.api_endpoint,
            )

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_make_request_success(self, mock_requests):
        """Test successful API request."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": {"volume": {"name": "test-vol"}}}

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client._make_request("GET", "/v1/volumes")

        assert result == {"data": {"volume": {"name": "test-vol"}}}
        mock_session.request.assert_called_once()

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_make_request_404_error(self, mock_requests):
        """Test API request with 404 error."""
        # Preserve real exceptions
        mock_requests.exceptions = requests.exceptions

        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"detail": "Volume not found"}
        mock_response.text = "Volume not found"

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)

        with pytest.raises(arca_exceptions.ArcaAPIError) as exc_info:
            client._make_request("GET", "/v1/volumes/test")

        assert exc_info.value.status_code == 404
        assert "Volume not found" in str(exc_info.value)

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_make_request_timeout(self, mock_requests):
        """Test API request timeout."""
        # Create a proper exception class
        class TimeoutException(Exception):
            pass

        mock_session = Mock()
        mock_session.request.side_effect = TimeoutException("Timeout")
        mock_requests.Session.return_value = mock_session
        mock_requests.exceptions.Timeout = TimeoutException

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)

        with pytest.raises(arca_exceptions.ArcaAPITimeout):
            client._make_request("GET", "/v1/volumes")

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_create_volume_success(self, mock_requests):
        """Test successful volume creation."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "volume": {
                    "name": "test-vol",
                    "svm": "test-svm",
                    "size_gib": 10,
                    "status": "available",
                }
            }
        }

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.create_volume(
            name="test-vol", svm="test-svm", size_gib=10, thin=True
        )

        assert result["name"] == "test-vol"
        assert result["svm"] == "test-svm"
        assert result["size_gib"] == 10

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_create_volume_already_exists(self, mock_requests):
        """Test volume creation with conflict error."""
        mock_requests.exceptions = requests.exceptions

        mock_response = Mock()
        mock_response.status_code = 409
        mock_response.json.return_value = {"detail": "Volume already exists"}
        mock_response.text = "Volume already exists"

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)

        with pytest.raises(arca_exceptions.ArcaVolumeAlreadyExists):
            client.create_volume(name="test-vol", svm="test-svm", size_gib=10)

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_create_volume_svm_not_found(self, mock_requests):
        """Test volume creation with SVM not found."""
        mock_requests.exceptions = requests.exceptions

        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"detail": "SVM not found"}
        mock_response.text = "SVM not found"

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)

        with pytest.raises(arca_exceptions.ArcaSVMNotFound):
            client.create_volume(name="test-vol", svm="nonexistent-svm", size_gib=10)

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_delete_volume_success(self, mock_requests):
        """Test successful volume deletion."""
        mock_response = Mock()
        mock_response.status_code = 204

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        client.delete_volume(name="test-vol", svm="test-svm")

        mock_session.request.assert_called_once()

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_delete_volume_not_found(self, mock_requests):
        """Test volume deletion with not found error."""
        mock_requests.exceptions = requests.exceptions

        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"detail": "Volume not found"}
        mock_response.text = "Volume not found"

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)

        with pytest.raises(arca_exceptions.ArcaVolumeNotFound):
            client.delete_volume(name="test-vol", svm="test-svm")

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_resize_volume_success(self, mock_requests):
        """Test successful volume resize."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "volume": {
                    "name": "test-vol",
                    "svm": "test-svm",
                    "size_gib": 20,
                }
            }
        }

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.resize_volume(name="test-vol", svm="test-svm", new_size_gib=20)

        assert result["size_gib"] == 20

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_create_export_success(self, mock_requests):
        """Test successful export creation."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "export": {
                    "svm": "test-svm",
                    "volume": "test-vol",
                    "client": "10.0.0.0/16",
                    "access": "rw",
                }
            }
        }

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.create_export(
            svm="test-svm", volume="test-vol", client="10.0.0.0/16", root_squash=False
        )

        assert result["svm"] == "test-svm"
        assert result["volume"] == "test-vol"
        assert result["access"] == "rw"

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_delete_export_success(self, mock_requests):
        """Test successful export deletion."""
        mock_response = Mock()
        mock_response.status_code = 204

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        client.delete_export(svm="test-svm", volume="test-vol", client="10.0.0.0/16")

        mock_session.request.assert_called_once()

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_list_svms_success(self, mock_requests):
        """Test successful SVM listing."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "items": [
                    {"name": "svm1", "vip": "192.168.100.5"},
                    {"name": "svm2", "vip": "192.168.100.6"},
                ]
            }
        }

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.list_svms()

        assert len(result) == 2
        assert result[0]["name"] == "svm1"
        assert result[1]["name"] == "svm2"

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_get_svm_success(self, mock_requests):
        """Test successful SVM retrieval."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {"items": [{"name": "test-svm", "vip": "192.168.100.5"}]}
        }

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.get_svm(name="test-svm")

        assert result["name"] == "test-svm"
        assert result["vip"] == "192.168.100.5"

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_get_svm_not_found(self, mock_requests):
        """Test SVM retrieval with not found."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": {"items": []}}

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)

        with pytest.raises(arca_exceptions.ArcaSVMNotFound):
            client.get_svm(name="nonexistent-svm")

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_context_manager(self, mock_requests):
        """Test client as context manager."""
        mock_session = Mock()
        mock_requests.Session.return_value = mock_session

        with arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint) as client:
            assert client.session is not None

        mock_session.close.assert_called_once()

    # Snapshot tests

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_create_snapshot_success(self, mock_requests):
        """Test successful snapshot creation."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "snapshot": {
                    "name": "snap1",
                    "svm": "test-svm",
                    "volume": "test-vol",
                    "status": "available",
                }
            }
        }

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.create_snapshot(name="snap1", svm="test-svm", volume="test-vol")

        assert result["name"] == "snap1"
        assert result["svm"] == "test-svm"
        assert result["volume"] == "test-vol"

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_create_snapshot_already_exists(self, mock_requests):
        """Test snapshot creation with conflict error."""
        mock_requests.exceptions = requests.exceptions

        mock_response = Mock()
        mock_response.status_code = 409
        mock_response.json.return_value = {"detail": "Snapshot already exists"}
        mock_response.text = "Snapshot already exists"

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)

        with pytest.raises(arca_exceptions.ArcaSnapshotAlreadyExists):
            client.create_snapshot(name="snap1", svm="test-svm", volume="test-vol")

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_delete_snapshot_success(self, mock_requests):
        """Test successful snapshot deletion."""
        mock_response = Mock()
        mock_response.status_code = 204

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        client.delete_snapshot(name="snap1", svm="test-svm", volume="test-vol")

        mock_session.request.assert_called_once()

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_delete_snapshot_not_found(self, mock_requests):
        """Test snapshot deletion with not found error."""
        mock_requests.exceptions = requests.exceptions

        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"detail": "Snapshot not found"}
        mock_response.text = "Snapshot not found"

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)

        with pytest.raises(arca_exceptions.ArcaSnapshotNotFound):
            client.delete_snapshot(name="snap1", svm="test-svm", volume="test-vol")

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_list_snapshots_success(self, mock_requests):
        """Test successful snapshot listing."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "items": [
                    {"name": "snap1", "svm": "test-svm", "volume": "vol1"},
                    {"name": "snap2", "svm": "test-svm", "volume": "vol1"},
                ]
            }
        }

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.list_snapshots(svm="test-svm", volume="vol1")

        assert len(result) == 2
        assert result[0]["name"] == "snap1"
        assert result[1]["name"] == "snap2"

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_create_volume_from_snapshot_success(self, mock_requests):
        """Test successful volume creation from snapshot."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "volume": {
                    "name": "new-vol",
                    "svm": "test-svm",
                    "size_gib": 10,
                    "status": "available",
                }
            }
        }

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.create_volume_from_snapshot(
            name="new-vol", svm="test-svm", snapshot="snap1", size_gib=10
        )

        assert result["name"] == "new-vol"
        assert result["svm"] == "test-svm"
        assert result["size_gib"] == 10

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_create_volume_from_snapshot_not_found(self, mock_requests):
        """Test volume creation from non-existent snapshot."""
        mock_requests.exceptions = requests.exceptions

        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"detail": "Snapshot not found"}
        mock_response.text = "Snapshot not found"

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)

        with pytest.raises(arca_exceptions.ArcaSnapshotNotFound):
            client.create_volume_from_snapshot(
                name="new-vol", svm="test-svm", snapshot="nonexistent", size_gib=10
            )

    # QoS tests

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_apply_qos_success(self, mock_requests):
        """Test successful QoS application."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "qos": {
                    "svm": "test-svm",
                    "volume": "test-vol",
                    "qos_enabled": True,
                    "read_iops": 5000,
                    "write_iops": 5000,
                }
            }
        }

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.apply_qos(
            volume="test-vol",
            svm="test-svm",
            read_iops=5000,
            write_iops=5000,
        )

        assert result["svm"] == "test-svm"
        assert result["volume"] == "test-vol"
        assert result["qos_enabled"] is True
        assert result["read_iops"] == 5000

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_apply_qos_volume_not_found(self, mock_requests):
        """Test QoS application with volume not found."""
        mock_requests.exceptions = requests.exceptions

        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"detail": "Volume not found"}
        mock_response.text = "Volume not found"

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)

        with pytest.raises(arca_exceptions.ArcaVolumeNotFound):
            client.apply_qos(
                volume="nonexistent",
                svm="test-svm",
                read_iops=5000,
            )

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_remove_qos_success(self, mock_requests):
        """Test successful QoS removal."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {"message": "QoS limits removed"}
        }

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        client.remove_qos(volume="test-vol", svm="test-svm")

        mock_session.request.assert_called_once()

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_get_qos_success(self, mock_requests):
        """Test successful QoS retrieval."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "qos": {
                    "svm": "test-svm",
                    "volume": "test-vol",
                    "qos_enabled": True,
                    "read_iops": 5000,
                    "write_iops": 5000,
                    "read_bps": 524288000,
                    "write_bps": 524288000,
                }
            }
        }

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.get_qos(volume="test-vol", svm="test-svm")

        assert result["qos_enabled"] is True
        assert result["read_iops"] == 5000
        assert result["write_iops"] == 5000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
