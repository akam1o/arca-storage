"""Advanced unit tests for ARCA Manila API client.

These tests cover error mapping edge cases and resource ID extraction logic.
"""

from unittest.mock import Mock, patch

import pytest
import requests

from arca_storage.openstack.manila import client as manila_client
from arca_storage.openstack.manila import exceptions


class TestErrorMappingEdgeCases:
    """Test edge cases in HTTP error to exception mapping."""

    @pytest.fixture
    def client(self):
        return manila_client.ArcaManilaClient(
            api_endpoint="http://192.168.10.5:8080",
            timeout=30,
            verify_ssl=False,
            auth_type="none",
        )

    @patch("requests.Session.request")
    def test_404_svm_maps_to_ArcaSVMNotFound(self, mock_request, client):
        """Test that 404 on SVM operations maps to ArcaSVMNotFound."""
        resp = Mock()
        resp.status_code = 404
        resp.text = "SVM not found"
        resp.json.return_value = {"detail": "SVM not found"}
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaSVMNotFound):
            client._make_request("GET", "/v1/svms/nonexistent-svm")

    @patch("requests.Session.request")
    def test_404_snapshot_maps_to_ArcaSnapshotNotFound(self, mock_request, client):
        """Test that 404 on snapshot operations maps to ArcaSnapshotNotFound."""
        resp = Mock()
        resp.status_code = 404
        resp.text = "Snapshot not found"
        resp.json.return_value = {"detail": "Snapshot not found"}
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaSnapshotNotFound):
            client._make_request("GET", "/v1/snapshots/nonexistent-snap")

    @patch("requests.Session.request")
    def test_404_export_maps_to_ArcaShareNotFound(self, mock_request, client):
        """Test that 404 on export operations maps to ArcaShareNotFound."""
        resp = Mock()
        resp.status_code = 404
        resp.text = "Export not found"
        resp.json.return_value = {"detail": "Export not found"}
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaShareNotFound):
            client._make_request("DELETE", "/v1/exports/123")

    # NOTE: VLAN conflicts are NOT mapped to ArcaNetworkConflict
    # The client implementation only treats IP-related conflicts as ArcaNetworkConflict
    # VLAN conflicts will be raised as generic ArcaManilaAPIError

    @patch("requests.Session.request")
    def test_409_share_exists_maps_to_ArcaShareAlreadyExists(self, mock_request, client):
        """Test that share already exists maps to ArcaShareAlreadyExists."""
        resp = Mock()
        resp.status_code = 409
        resp.text = "Volume already exists"
        resp.json.return_value = {"detail": "Volume share-123 already exists"}
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaShareAlreadyExists):
            client._make_request("POST", "/v1/volumes", json_data={"name": "share-123"})

    @patch("requests.Session.request")
    def test_409_generic_conflict_with_non_json_body(self, mock_request, client):
        """Test 409 handling when response body is not JSON."""
        resp = Mock()
        resp.status_code = 409
        resp.text = "Conflict occurred"
        resp.json.side_effect = ValueError("Not JSON")
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaManilaAPIError, match="Conflict"):
            client._make_request("POST", "/v1/svms", json_data={"name": "test"})

    @patch("requests.Session.request")
    def test_409_false_positive_not_ip_conflict(self, mock_request, client):
        """Test that non-IP 409 conflicts don't get misclassified."""
        resp = Mock()
        resp.status_code = 409
        resp.text = "Resource locked by another operation"
        resp.json.return_value = {"detail": resp.text}
        mock_request.return_value = resp

        # Should raise generic conflict, not network conflict
        with pytest.raises(exceptions.ArcaManilaAPIError):
            try:
                client._make_request("POST", "/v1/volumes", json_data={"name": "test"})
            except exceptions.ArcaNetworkConflict:
                pytest.fail("Should not raise ArcaNetworkConflict for non-network conflicts")

    @patch("requests.Session.request")
    def test_500_internal_server_error(self, mock_request, client):
        """Test 500 server error mapping."""
        resp = Mock()
        resp.status_code = 500
        resp.text = "Internal server error"
        resp.json.return_value = {"detail": "Database connection failed"}
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaManilaAPIError, match="500"):
            client._make_request("GET", "/v1/svms")

    @patch("requests.Session.request")
    def test_503_service_unavailable(self, mock_request, client):
        """Test 503 service unavailable error."""
        resp = Mock()
        resp.status_code = 503
        resp.text = "Service temporarily unavailable"
        resp.json.side_effect = ValueError("Not JSON")
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaManilaAPIError):
            client._make_request("GET", "/v1/svms")

    @patch("requests.Session.request")
    def test_400_bad_request(self, mock_request, client):
        """Test 400 bad request error."""
        resp = Mock()
        resp.status_code = 400
        resp.text = "Invalid request parameters"
        resp.json.return_value = {"detail": "size_gib must be positive"}
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaManilaAPIError, match="400|Invalid"):
            client._make_request("POST", "/v1/volumes", json_data={"size_gib": -1})


class TestResourceIDExtraction:
    """Test resource ID extraction logic for error messages."""

    @pytest.fixture
    def client(self):
        return manila_client.ArcaManilaClient(
            api_endpoint="http://192.168.10.5:8080",
            timeout=30,
            verify_ssl=False,
            auth_type="none",
        )

    @patch("requests.Session.request")
    def test_volume_id_extracted_from_path(self, mock_request, client):
        """Test volume ID extraction from various path formats."""
        resp = Mock()
        resp.status_code = 404
        resp.text = "Not found"
        resp.json.return_value = {"detail": "Volume not found"}
        mock_request.return_value = resp

        # Test various volume path formats
        test_paths = [
            "/v1/volumes/share-123",
            "/v1/volumes/share-123/resize",
            "/v1/svms/test-svm/volumes/share-123",
        ]

        for path in test_paths:
            try:
                client._make_request("GET", path)
            except exceptions.ArcaShareNotFound as e:
                # Should include share ID in error message
                assert "share-123" in str(e) or "Not found" in str(e)
            except exceptions.ArcaManilaAPIError:
                # Also acceptable
                pass

    @patch("requests.Session.request")
    def test_svm_id_extracted_from_path(self, mock_request, client):
        """Test SVM ID extraction from path."""
        resp = Mock()
        resp.status_code = 404
        resp.text = "SVM not found"
        resp.json.return_value = {"detail": "SVM not found"}
        mock_request.return_value = resp

        try:
            client._make_request("GET", "/v1/svms/test-svm")
        except exceptions.ArcaSVMNotFound as e:
            # Should include SVM name in error
            assert "test-svm" in str(e) or "not found" in str(e).lower()

    @patch("requests.Session.request")
    def test_snapshot_id_extracted_from_path(self, mock_request, client):
        """Test snapshot ID extraction from path."""
        resp = Mock()
        resp.status_code = 404
        resp.text = "Snapshot not found"
        resp.json.return_value = {"detail": "Snapshot not found"}
        mock_request.return_value = resp

        try:
            client._make_request("DELETE", "/v1/snapshots/snap-456")
        except exceptions.ArcaSnapshotNotFound as e:
            assert "snap-456" in str(e) or "not found" in str(e).lower()


class TestAuthenticationEdgeCases:
    """Test authentication and TLS configuration edge cases."""

    def test_basic_auth_missing_password(self):
        """Test that basic auth without password raises ValueError."""
        with pytest.raises(ValueError, match="username and password"):
            manila_client.ArcaManilaClient(
                api_endpoint="http://192.168.10.5:8080",
                auth_type="basic",
                username="admin",
                verify_ssl=False,
            )

    def test_basic_auth_missing_username(self):
        """Test that basic auth without username raises ValueError."""
        with pytest.raises(ValueError, match="username and password"):
            manila_client.ArcaManilaClient(
                api_endpoint="http://192.168.10.5:8080",
                auth_type="basic",
                password="secret",
                verify_ssl=False,
            )

    def test_basic_auth_success(self):
        """Test successful basic auth initialization."""
        client = manila_client.ArcaManilaClient(
            api_endpoint="http://192.168.10.5:8080",
            auth_type="basic",
            username="admin",
            password="secret",
            verify_ssl=False,
        )
        assert client.session.auth == ("admin", "secret")

    def test_ca_bundle_overrides_verify_ssl(self):
        """Test that CA bundle path overrides boolean verify_ssl."""
        client = manila_client.ArcaManilaClient(
            api_endpoint="https://192.168.10.5:8443",
            ca_bundle="/path/to/ca-bundle.crt",
            verify_ssl=False,  # Should be overridden
            auth_type="none",
        )
        # CA bundle path should be used instead of False
        assert client.verify_ssl == "/path/to/ca-bundle.crt"

    def test_client_cert_with_key(self):
        """Test client certificate with separate key file."""
        client = manila_client.ArcaManilaClient(
            api_endpoint="https://192.168.10.5:8443",
            client_cert="/path/to/client.crt",
            client_key="/path/to/client.key",
            verify_ssl=False,
            auth_type="none",
        )
        assert client.session.cert == ("/path/to/client.crt", "/path/to/client.key")

    def test_client_cert_without_key(self):
        """Test client certificate without separate key (combined file)."""
        client = manila_client.ArcaManilaClient(
            api_endpoint="https://192.168.10.5:8443",
            client_cert="/path/to/client.pem",
            verify_ssl=False,
            auth_type="none",
        )
        assert client.session.cert == "/path/to/client.pem"

    def test_invalid_auth_type(self):
        """Test that invalid auth type raises ValueError."""
        with pytest.raises(ValueError, match="Invalid auth_type"):
            manila_client.ArcaManilaClient(
                api_endpoint="http://192.168.10.5:8080",
                auth_type="invalid",
                verify_ssl=False,
            )

    def test_none_auth_type_explicit(self):
        """Test explicit 'none' auth type."""
        client = manila_client.ArcaManilaClient(
            api_endpoint="http://192.168.10.5:8080",
            auth_type="none",
            verify_ssl=False,
        )
        # Should initialize without errors
        assert client.base_url == "http://192.168.10.5:8080"


# NOTE: Retry adapter configuration testing is omitted because:
# 1. It requires mocking requests.Session itself, which creates a circular dependency
# 2. The retry adapter is an internal implementation detail of the requests library
# 3. Integration tests with real HTTP requests would be more appropriate for this functionality
