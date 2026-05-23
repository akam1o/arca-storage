"""Unit tests for ARCA Manila API client."""

from unittest.mock import Mock, call, patch

import pytest
import requests  # type: ignore[import-untyped]

from arca_storage.openstack.manila import client as manila_client
from arca_storage.openstack.manila import exceptions


class TestArcaManilaClientInit:
    def test_init_with_none_auth(self):
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
            api_endpoint="https://192.168.10.5:8443",
            auth_type="token",
            api_token="test-token-123",
            verify_ssl=False,
        )
        assert client.session.headers["Authorization"] == "Bearer test-token-123"

    def test_init_with_token_auth_trims_token(self):
        client = manila_client.ArcaManilaClient(
            api_endpoint="https://192.168.10.5:8443",
            auth_type="token",
            api_token=" test-token-123 \n",
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

    def test_init_token_auth_rejects_blank_token(self):
        with pytest.raises(ValueError, match="api_token is required"):
            manila_client.ArcaManilaClient(
                api_endpoint="http://127.0.0.1:8080",
                auth_type="token",
                api_token=" \t\n ",
                verify_ssl=False,
            )

    def test_init_rejects_remote_http_token_without_opt_in(self):
        with pytest.raises(ValueError, match="remote plain HTTP"):
            manila_client.ArcaManilaClient(
                api_endpoint="http://192.168.10.5:8080",
                auth_type="token",
                api_token="test-token-123",
                verify_ssl=False,
            )

    def test_init_allows_remote_http_token_with_explicit_opt_in(self):
        client = manila_client.ArcaManilaClient(
            api_endpoint="http://192.168.10.5:8080",
            auth_type="token",
            api_token="test-token-123",
            verify_ssl=False,
            allow_insecure_token_transport=True,
        )

        assert client.session.headers["Authorization"] == "Bearer test-token-123"


class TestArcaManilaClientMakeRequest:
    @pytest.fixture
    def client(self):
        return manila_client.ArcaManilaClient(
            api_endpoint="http://192.168.10.5:8080",
            timeout=30,
            verify_ssl=False,
            auth_type="none",
        )

    def _render_log_calls(self, mock_log):
        return " ".join(str(call.args) for call in mock_log.call_args_list)

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
    def test_connection_error_redacts_sensitive_details(self, mock_request, client):
        mock_request.side_effect = requests.exceptions.ConnectionError("Authorization: Bearer secret-token")
        with pytest.raises(exceptions.ArcaAPIConnectionError) as exc_info:
            client._make_request("GET", "/v1/svms")

        assert "secret-token" not in str(exc_info.value)
        assert "<redacted>" in str(exc_info.value)

    @patch("requests.Session.request")
    def test_debug_log_redacts_sensitive_request_fields(self, mock_request, client):
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {"data": {}}
        mock_request.return_value = resp

        with patch.object(manila_client.LOG, "debug") as mock_debug:
            client._make_request(
                "POST",
                "/v1/volumes/share-secret-token?password=hunter2",
                json_data={"name": "svm1", "auth_token": "secret-token"},
                params={"password": "hunter2"},
            )

        rendered_calls = self._render_log_calls(mock_debug)
        assert "share-secret-token" not in rendered_calls
        assert "secret-token" not in rendered_calls
        assert "hunter2" not in rendered_calls
        assert "/v1/volumes/<id>" in rendered_calls
        assert "<redacted>" in rendered_calls

    @patch("requests.Session.request")
    def test_404_volume_maps_to_ArcaShareNotFound(self, mock_request, client):
        resp = Mock()
        resp.status_code = 404
        resp.text = "not found"
        resp.json.return_value = {"detail": "not found"}
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaShareNotFound):
            client._make_request("GET", "/v1/volumes/share-123")

    def test_extract_resource_id_decodes_url_quoted_segments(self, client):
        assert client._extract_resource_id("/v1/volumes/share%2F..%2Fqos", "GET") == "share/../qos"

    def test_extract_resource_id_ignores_query_string(self, client):
        assert client._extract_resource_id("/v1/volumes/share-123?password=hunter2", "GET") == "share-123"

    @patch("requests.Session.request")
    def test_warning_log_redacts_path_and_response_details(self, mock_request, client):
        resp = Mock()
        resp.status_code = 404
        resp.text = "not found token=secret-token password=hunter2"
        resp.json.return_value = {"detail": resp.text}
        mock_request.return_value = resp

        with patch.object(manila_client.LOG, "warning") as mock_warning:
            with pytest.raises(exceptions.ArcaShareNotFound):
                client._make_request("GET", "/v1/volumes/share-private-id?password=hunter2")

        rendered_calls = self._render_log_calls(mock_warning)
        assert "share-private-id" not in rendered_calls
        assert "secret-token" not in rendered_calls
        assert "hunter2" not in rendered_calls
        assert "/v1/volumes/<id>" in rendered_calls

    @patch("requests.Session.request")
    def test_409_ip_conflict_maps_to_ArcaNetworkConflict(self, mock_request, client):
        resp = Mock()
        resp.status_code = 409
        resp.text = "IP address 192.168.100.10 is already in use"
        resp.json.return_value = {"detail": resp.text}
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaNetworkConflict):
            client._make_request("POST", "/v1/svms", json_data={"name": "svm1", "ip_cidr": "192.168.100.10/24"})

    @patch("requests.Session.request")
    def test_api_error_redacts_sensitive_response_details(self, mock_request, client):
        resp = Mock()
        resp.status_code = 500
        resp.text = "backend failed token=secret-token password=hunter2"
        resp.json.return_value = {"detail": resp.text, "auth_token": "secret-token"}
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaManilaAPIError) as exc_info:
            client._make_request("POST", "/v1/svms", json_data={"name": "svm1"})

        assert "secret-token" not in str(exc_info.value)
        assert "hunter2" not in str(exc_info.value)
        assert "<redacted>" in str(exc_info.value)

    @patch("requests.Session.request")
    def test_error_log_redacts_path_and_response_details(self, mock_request, client):
        resp = Mock()
        resp.status_code = 500
        resp.text = "backend failed token=secret-token password=hunter2"
        resp.json.return_value = {"detail": resp.text}
        mock_request.return_value = resp

        with patch.object(manila_client.LOG, "error") as mock_error:
            with pytest.raises(exceptions.ArcaManilaAPIError):
                client._make_request("POST", "/v1/svms/svm-private-id?password=hunter2")

        rendered_calls = self._render_log_calls(mock_error)
        assert "svm-private-id" not in rendered_calls
        assert "secret-token" not in rendered_calls
        assert "hunter2" not in rendered_calls
        assert "backend failed" not in rendered_calls
        assert "/v1/svms/<id>" in rendered_calls

    @patch("requests.Session.request")
    def test_409_network_conflict_redacts_sensitive_details(self, mock_request, client):
        resp = Mock()
        resp.status_code = 409
        resp.text = "IP address 192.168.100.10 is already in use token=secret-token"
        resp.json.return_value = {"detail": resp.text}
        mock_request.return_value = resp

        with pytest.raises(exceptions.ArcaNetworkConflict) as exc_info:
            client._make_request("POST", "/v1/svms", json_data={"name": "svm1"})

        assert "secret-token" not in str(exc_info.value)
        assert "192.168.100.10" in str(exc_info.value)


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

    def test_resource_path_segments_are_url_quoted(self, client):
        with patch.object(client, "_make_request") as mock_make:
            mock_make.return_value = {}
            client.delete_volume(name="share/../qos", svm="svm1")

        mock_make.assert_called_once_with(
            "DELETE",
            "/v1/volumes/share%2F..%2Fqos",
            params={"svm": "svm1", "force": "false"},
        )

    def test_list_volumes_follows_pagination(self, client):
        with patch.object(client, "_make_request") as mock_make:
            mock_make.side_effect = [
                {"data": {"items": [{"name": "share-123"}], "next_cursor": "cursor-1"}},
                {"data": {"items": [{"name": "share-456"}], "next_cursor": None}},
            ]

            result = client.list_volumes(svm="svm1", name="share-123")

        assert result == [{"name": "share-123"}, {"name": "share-456"}]
        mock_make.assert_has_calls(
            [
                call("GET", "/v1/volumes", params={"limit": 200, "svm": "svm1", "name": "share-123"}),
                call(
                    "GET",
                    "/v1/volumes",
                    params={"limit": 200, "svm": "svm1", "name": "share-123", "cursor": "cursor-1"},
                ),
            ]
        )

    def test_clone_volume_from_snapshot_uses_api_snapshot_field(self, client):
        with patch.object(client, "_make_request") as mock_make:
            mock_make.return_value = {"data": {"volume": {"name": "share-new"}}}
            client.clone_volume_from_snapshot(
                name="share-new",
                svm="svm1",
                source_volume="share-src",
                snapshot_name="snap1",
                size_gib=12,
            )
            mock_make.assert_called_once_with(
                "POST",
                "/v1/volumes/share-src/clone",
                json_data={"name": "share-new", "svm": "svm1", "snapshot": "snap1", "size_gib": 12},
            )

    def test_list_exports_passes_filters(self, client):
        with patch.object(client, "_make_request") as mock_make:
            mock_make.return_value = {"data": {"items": []}}
            client.list_exports(svm="svm1", volume="share-123")
            mock_make.assert_called_once_with(
                "GET", "/v1/exports", params={"limit": 200, "svm": "svm1", "volume": "share-123"}
            )

    def test_list_exports_follows_pagination(self, client):
        with patch.object(client, "_make_request") as mock_make:
            mock_make.side_effect = [
                {"data": {"items": [{"client": "10.0.0.0/24"}], "next_cursor": "cursor-1"}},
                {"data": {"items": [{"client": "10.0.1.0/24"}], "next_cursor": None}},
            ]

            result = client.list_exports(svm="svm1", volume="share-123")

        assert result == [{"client": "10.0.0.0/24"}, {"client": "10.0.1.0/24"}]
        mock_make.assert_has_calls(
            [
                call("GET", "/v1/exports", params={"limit": 200, "svm": "svm1", "volume": "share-123"}),
                call(
                    "GET",
                    "/v1/exports",
                    params={"limit": 200, "svm": "svm1", "volume": "share-123", "cursor": "cursor-1"},
                ),
            ]
        )

    def test_list_snapshots_follows_pagination(self, client):
        with patch.object(client, "_make_request") as mock_make:
            mock_make.side_effect = [
                {"data": {"items": [{"name": "snap1"}], "next_cursor": "cursor-1"}},
                {"data": {"items": [{"name": "snap2"}], "next_cursor": None}},
            ]

            result = client.list_snapshots(svm="svm1", volume="share-123")

        assert result == [{"name": "snap1"}, {"name": "snap2"}]
        mock_make.assert_has_calls(
            [
                call("GET", "/v1/snapshots", params={"limit": 200, "svm": "svm1", "volume": "share-123"}),
                call(
                    "GET",
                    "/v1/snapshots",
                    params={"limit": 200, "svm": "svm1", "volume": "share-123", "cursor": "cursor-1"},
                ),
            ]
        )

    def test_list_svms_follows_pagination(self, client):
        with patch.object(client, "_make_request") as mock_make:
            mock_make.side_effect = [
                {"data": {"items": [{"name": "svm1"}], "next_cursor": "cursor-1"}},
                {"data": {"items": [{"name": "svm2"}], "next_cursor": None}},
            ]

            result = client.list_svms()

        assert result == [{"name": "svm1"}, {"name": "svm2"}]
        mock_make.assert_has_calls(
            [
                call("GET", "/v1/svms", params={"limit": 200}),
                call("GET", "/v1/svms", params={"limit": 200, "cursor": "cursor-1"}),
            ]
        )

    @pytest.mark.parametrize(
        ("method_name", "kwargs", "resource"),
        [
            ("list_volumes", {"svm": "svm1", "name": "share-123"}, "volume"),
            ("list_exports", {"svm": "svm1", "volume": "share-123"}, "export"),
            ("list_snapshots", {"svm": "svm1", "volume": "share-123"}, "snapshot"),
            ("list_svms", {}, "SVM"),
        ],
    )
    def test_list_methods_reject_repeated_pagination_cursor(
        self, client, method_name, kwargs, resource
    ):
        with patch.object(client, "_make_request") as mock_make:
            mock_make.side_effect = [
                {"data": {"items": [{"name": "first"}], "next_cursor": "cursor-1"}},
                {"data": {"items": [{"name": "second"}], "next_cursor": "cursor-1"}},
            ]

            method = getattr(client, method_name)
            with pytest.raises(
                exceptions.ArcaManilaAPIError,
                match=f"Repeated {resource} pagination cursor",
            ):
                method(**kwargs)

        assert mock_make.call_count == 2
