"""Integration tests for optional API bearer-token authentication."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from arca_storage.api.auth import (
    UNKNOWN_SERVER_HOST,
    configured_api_token,
    insecure_remote_api_allowed,
    non_loopback_request_server_host,
    unauthenticated_loopback_allowed,
)
from arca_storage.api.main import app


def test_api_auth_requires_token_without_loopback_opt_out(monkeypatch):
    monkeypatch.delenv("ARCA_API_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_ALLOW_UNAUTHENTICATED_LOOPBACK", raising=False)

    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        response = client.get("/v1/svms")

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "AUTH_TOKEN_REQUIRED"
    assert payload["error"]["details"]["host"] == "loopback"


def test_api_auth_allows_loopback_without_token_when_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("ARCA_API_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ARCA_ALLOW_UNAUTHENTICATED_LOOPBACK", "true")

    with patch("arca_storage.api.services.svm_service.list_svms", return_value={"items": [], "next_cursor": None}):
        with TestClient(app, base_url="http://127.0.0.1:8080") as client:
            response = client.get("/v1/svms")

    assert response.status_code == 200


def test_api_auth_rejects_missing_bearer_token(monkeypatch):
    monkeypatch.setenv("ARCA_API_TOKEN", "secret-token")

    with TestClient(app) as client:
        response = client.get("/v1/svms")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_api_auth_accepts_configured_bearer_token(monkeypatch):
    monkeypatch.setenv("ARCA_API_TOKEN", "secret-token")

    with patch("arca_storage.api.services.svm_service.list_svms", return_value={"items": [], "next_cursor": None}):
        with TestClient(app) as client:
            response = client.get("/v1/svms", headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200


def test_configured_api_token_trims_and_ignores_blank_values(monkeypatch):
    monkeypatch.setenv("ARCA_API_TOKEN", " \n\t ")
    monkeypatch.setenv("ARCA_AUTH_TOKEN", " fallback-token \n")

    assert configured_api_token() == "fallback-token"


def test_api_auth_rejects_blank_configured_token(monkeypatch):
    monkeypatch.setenv("ARCA_API_TOKEN", " \t ")
    monkeypatch.delenv("ARCA_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_ALLOW_UNAUTHENTICATED_LOOPBACK", raising=False)

    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        response = client.get("/v1/svms", headers={"Authorization": "Bearer  "})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUTH_TOKEN_REQUIRED"


def test_api_auth_accepts_trimmed_configured_bearer_token(monkeypatch):
    monkeypatch.setenv("ARCA_API_TOKEN", " secret-token \n")

    with patch("arca_storage.api.services.svm_service.list_svms", return_value={"items": [], "next_cursor": None}):
        with TestClient(app) as client:
            response = client.get("/v1/svms", headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200


def test_api_auth_protects_openapi_schema(monkeypatch):
    monkeypatch.setenv("ARCA_API_TOKEN", "secret-token")

    with TestClient(app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 401


def test_api_auth_accepts_openapi_schema_with_bearer_token(monkeypatch):
    monkeypatch.setenv("ARCA_API_TOKEN", "secret-token")

    with TestClient(app) as client:
        response = client.get("/openapi.json", headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200


def test_api_auth_rejects_non_loopback_request_without_token(monkeypatch):
    monkeypatch.delenv("ARCA_API_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ARCA_ALLOW_UNAUTHENTICATED_LOOPBACK", "true")

    with TestClient(app, base_url="http://192.0.2.10:8080") as client:
        response = client.get("/v1/svms")

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "AUTH_TOKEN_REQUIRED"
    assert payload["error"]["details"]["host"] == "192.0.2.10"


def test_api_auth_rejects_unknown_request_host_without_token(monkeypatch):
    monkeypatch.delenv("ARCA_API_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ARCA_ALLOW_UNAUTHENTICATED_LOOPBACK", "true")

    with TestClient(app, base_url="http://testserver") as client:
        response = client.get("/v1/svms")

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "AUTH_TOKEN_REQUIRED"
    assert payload["error"]["details"]["host"] == "testserver"


def test_non_loopback_request_server_host_fails_closed_for_missing_scope():
    assert non_loopback_request_server_host({}) == UNKNOWN_SERVER_HOST


def test_unauthenticated_loopback_allowed_requires_truthy_opt_in(monkeypatch):
    monkeypatch.delenv("ARCA_ALLOW_UNAUTHENTICATED_LOOPBACK", raising=False)
    assert unauthenticated_loopback_allowed() is False

    monkeypatch.setenv("ARCA_ALLOW_UNAUTHENTICATED_LOOPBACK", "true")
    assert unauthenticated_loopback_allowed() is True


def test_insecure_remote_api_allowed_requires_truthy_opt_in(monkeypatch):
    monkeypatch.delenv("ARCA_ALLOW_INSECURE_REMOTE_API", raising=False)
    assert insecure_remote_api_allowed() is False

    monkeypatch.setenv("ARCA_ALLOW_INSECURE_REMOTE_API", "true")
    assert insecure_remote_api_allowed() is True
