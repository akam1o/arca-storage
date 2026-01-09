"""Unit tests for ARCA Manila API client."""

from unittest.mock import Mock, patch

import pytest
import requests

from arca_storage.openstack.manila import client as manila_client
from arca_storage.openstack.manila import exceptions


class TestArcaManilaClientInit:
    def test_init_basic(self):
        client = manila_client.ArcaManilaClient(
            api_endpoint="http://192.168.10.5:8080",
            timeout=30,
            retry_count=3,
            verify_ssl=False,
            auth_type="none",
        )
        assert client.base_url == "http://192.168.10.5:8080"
        assert client.timeout == 30
        assert client.retry_count == 3
        assert client.verify_ssl is False

    def test_init_with_trailing_slash(self):
        client = manila_client.ArcaManilaClient(
            api_endpoint="http://192.168.10.5:8080/",
            verify_ssl=False,
            auth_type="none",
        )
        assert client.base_url == "http://192.168.10.5:8080"

    def test_init_with_token_auth(self):
        client = manila_client.ArcaManilaClient(
            api_endpoint="http://192.168.10.5:8080",
            auth_type="token",
            api_token="test-token-123",
            verify_ssl=False,
        )
        assert client.session.headers["Authorization"] == "Bearer test-token-123"

    def test_init_token_auth_missing_token(self):
        with pytest.raises(ValueError, match="api_token is required"):
            manila_client.ArcaManilaClient(
                api_endpoint="http://192.168.10.5:8080",
                auth_type="token",
                verify_ssl=False,
            )


class TestArcaManilaClientMakeRequest:
    @pytest.fixture
    def client(self):
        return manila_client.ArcaManilaClient(
            api_endpoint="http://192.168.10.5:8080",
            timeout=30,
            verify_ssl=False,
            auth_type="none",
        )

    @patch("requests.Session.request")
    def test_timeout_maps_to_ArcaAPITimeout(self, mock_request, client):
        mock_request.side_effect = requests.exceptions.Timeout("timeout")
        with pytest.raises(exceptions.ArcaAPITimeout):
            client._make_request("GET", "/v1/svms")

    @patch("requests.Session.request")
    def test_connection_error_maps_to_ArcaAPIConnectionError(self, mock_request, client):
        mock_request.side_effect = requests.exceptions.ConnectionError("refused")
        with pytest.raises(exceptions.ArcaAPIConnectionError):
            client._make_request("GET", "/v1/svms")

    @patch("requests.Session.request")
    def test_404_volume_maps_to_ArcaShareNotFound(self, mock_request, client):
        resp = Mock()
        resp.status_code = 404
        resp.text = "not found"
        resp.json.return_value = {"detail": "not found"}
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaShareNotFound):
            client._make_request("GET", "/v1/volumes/share-123")

    @patch("requests.Session.request")
    def test_409_ip_conflict_maps_to_ArcaNetworkConflict(self, mock_request, client):
        resp = Mock()
        resp.status_code = 409
        resp.text = "IP address 192.168.100.10 is already in use"
        resp.json.return_value = {"detail": resp.text}
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaNetworkConflict):
            client._make_request("POST", "/v1/svms", json_data={"name": "svm1", "ip_cidr": "192.168.100.10/24"})


class TestArcaManilaClientOperations:
    @pytest.fixture
    def client(self):
        return manila_client.ArcaManilaClient(
            api_endpoint="http://192.168.10.5:8080",
            timeout=30,
            verify_ssl=False,
            auth_type="none",
        )

    def test_create_volume_returns_volume(self, client):
        with patch.object(client, "_make_request") as mock_make:
            mock_make.return_value = {"data": {"volume": {"name": "share-123", "export_path": "vip:/path"}}}
            vol = client.create_volume(name="share-123", svm="svm1", size_gib=10)
            assert vol["name"] == "share-123"
            assert vol["export_path"] == "vip:/path"

    def test_list_exports_passes_filters(self, client):
        with patch.object(client, "_make_request") as mock_make:
            mock_make.return_value = {"data": {"items": []}}
            client.list_exports(svm="svm1", volume="share-123")
            mock_make.assert_called_once_with("GET", "/v1/exports", params={"svm": "svm1", "volume": "share-123"})

