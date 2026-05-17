"""Unit tests for ARCA Storage API client."""

import unittest
from unittest.mock import Mock, patch

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
    def test_make_request_preserves_base_url_path_prefix(self, mock_requests):
        """Test API request under a reverse-proxy path prefix."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": {"items": []}}

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint="https://storage.example.com/arca")
        client._make_request("GET", "/v1/volumes")

        assert mock_session.request.call_args.kwargs["url"] == "https://storage.example.com/arca/v1/volumes"

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
    def test_make_request_redacts_sensitive_error_details(self, mock_requests):
        """API errors should not leak tokens or passwords into exceptions."""
        mock_requests.exceptions = requests.exceptions

        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.json.return_value = {
            "error": {
                "message": "backend failed with bearer secret-token and password=hunter2",
                "details": {"auth_token": "secret-token", "safe": "kept"},
            }
        }
        mock_response.text = "backend failed with bearer secret-token and password=hunter2"

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)

        with pytest.raises(arca_exceptions.ArcaAPIError) as exc_info:
            client._make_request("GET", "/v1/volumes")

        assert "secret-token" not in str(exc_info.value)
        assert "hunter2" not in str(exc_info.value)
        assert exc_info.value.response_data["error"]["details"]["auth_token"] == "<redacted>"
        assert exc_info.value.response_data["error"]["details"]["safe"] == "kept"

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_connection_error_redacts_sensitive_details(self, mock_requests):
        """Request exceptions should be redacted before surfacing to Cinder."""
        mock_requests.exceptions = requests.exceptions

        mock_session = Mock()
        mock_session.request.side_effect = requests.exceptions.ConnectionError(
            "Authorization: Bearer secret-token"
        )
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)

        with pytest.raises(arca_exceptions.ArcaAPIConnectionError) as exc_info:
            client._make_request("GET", "/v1/volumes")

        assert "secret-token" not in str(exc_info.value)
        assert "<redacted>" in str(exc_info.value)

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
    def test_list_volumes_follows_pagination(self, mock_requests):
        """Test volume listing follows cursor pagination."""
        first_response = Mock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "data": {"items": [{"name": "vol1"}], "next_cursor": "cursor-1"}
        }
        second_response = Mock()
        second_response.status_code = 200
        second_response.json.return_value = {
            "data": {"items": [{"name": "vol2"}], "next_cursor": None}
        }

        mock_session = Mock()
        mock_session.request.side_effect = [first_response, second_response]
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.list_volumes(svm="test-svm", limit=1)

        assert [item["name"] for item in result] == ["vol1", "vol2"]
        assert mock_session.request.call_count == 2
        assert mock_session.request.call_args_list[0].kwargs["params"] == {
            "svm": "test-svm",
            "limit": 1,
        }
        assert mock_session.request.call_args_list[1].kwargs["params"] == {
            "svm": "test-svm",
            "limit": 1,
            "cursor": "cursor-1",
        }

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_list_exports_follows_pagination(self, mock_requests):
        """Test export listing follows cursor pagination."""
        first_response = Mock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "data": {"items": [{"client": "10.0.0.0/24"}], "next_cursor": "cursor-1"}
        }
        second_response = Mock()
        second_response.status_code = 200
        second_response.json.return_value = {
            "data": {"items": [{"client": "10.0.1.0/24"}], "next_cursor": None}
        }

        mock_session = Mock()
        mock_session.request.side_effect = [first_response, second_response]
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.list_exports(svm="test-svm", volume="test-vol", limit=1)

        assert [item["client"] for item in result] == ["10.0.0.0/24", "10.0.1.0/24"]
        assert mock_session.request.call_count == 2
        assert mock_session.request.call_args_list[1].kwargs["params"]["cursor"] == "cursor-1"

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
    def test_list_svms_follows_pagination(self, mock_requests):
        """Test SVM listing follows cursor pagination."""
        first_response = Mock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "data": {"items": [{"name": "svm1"}], "next_cursor": "cursor-1"}
        }
        second_response = Mock()
        second_response.status_code = 200
        second_response.json.return_value = {
            "data": {"items": [{"name": "svm2"}], "next_cursor": None}
        }

        mock_session = Mock()
        mock_session.request.side_effect = [first_response, second_response]
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.list_svms(limit=1)

        assert [item["name"] for item in result] == ["svm1", "svm2"]
        assert mock_session.request.call_count == 2
        assert mock_session.request.call_args_list[1].kwargs["params"] == {
            "limit": 1,
            "cursor": "cursor-1",
        }

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
    def test_get_svm_capacity_success(self, mock_requests):
        """Test successful SVM capacity retrieval."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "capacity": {
                    "svm": "test-svm",
                    "total_gb": 1000,
                    "free_gb": 750,
                    "provisioned_gb": 125,
                }
            }
        }

        mock_session = Mock()
        mock_session.request.return_value = mock_response
        mock_requests.Session.return_value = mock_session

        client = arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint)
        result = client.get_svm_capacity("test-svm")

        assert result["total_gb"] == 1000
        assert result["free_gb"] == 750
        assert mock_session.request.call_args.kwargs["url"].endswith("/v1/svms/test-svm/capacity")

    @patch("arca_storage.openstack.cinder.client.requests")
    def test_context_manager(self, mock_requests):
        """Test client as context manager."""
        mock_session = Mock()
        mock_requests.Session.return_value = mock_session

        with arca_client.ArcaStorageClient(api_endpoint=self.api_endpoint) as client:
            assert client.session is not None

        mock_session.close.assert_called_once()

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
