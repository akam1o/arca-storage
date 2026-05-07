"""Integration tests for optional API bearer-token authentication."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from arca_storage.api.main import app


def test_api_auth_is_disabled_without_token(monkeypatch):
    monkeypatch.delenv("ARCA_API_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_AUTH_TOKEN", raising=False)

    with patch("arca_storage.api.services.svm_service.list_svms", return_value={"items": [], "next_cursor": None}):
        with TestClient(app) as client:
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


def test_api_auth_leaves_docs_open(monkeypatch):
    monkeypatch.setenv("ARCA_API_TOKEN", "secret-token")

    with TestClient(app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
