"""
Integration tests for API validation errors raised below request parsing.
"""

import logging
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from arca_storage.api.main import app
from arca_storage.errors import InternalError


@pytest.mark.integration
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/svms/!bad"),
        ("delete", "/v1/volumes/vol1?svm=!bad"),
        ("delete", "/v1/exports?svm=!bad&volume=vol1&client=10.0.0.0/24"),
    ],
)
def test_path_and_query_validation_errors_return_invalid_argument(method: str, path: str):
    client = TestClient(app, raise_server_exceptions=False)

    response = getattr(client, method)(path)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.integration
def test_export_body_name_validation_errors_return_invalid_argument():
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/v1/exports",
        json={"svm": "!bad", "volume": "vol1", "client": "10.0.0.0/24"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.integration
def test_body_name_validation_rejects_trailing_newline():
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/v1/svms",
        json={"name": "tenant\n", "ip_cidr": "192.168.10.5/24"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.integration
def test_request_validation_errors_do_not_echo_invalid_inputs():
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/v1/svms",
        json={"name": "secret-token\n", "ip_cidr": "192.168.10.5/24"},
    )

    payload = response.json()
    errors = payload["error"]["details"]["errors"]
    assert response.status_code == 400
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert "input" not in errors[0]
    assert "secret-token" not in str(payload)


@pytest.mark.integration
def test_request_validation_error_messages_do_not_echo_nested_input_values():
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/v1/exports",
        json={
            "svm": "tenant_a",
            "volume": "vol1",
            "client": "10.0.0.0/24",
            "sec": ["sys", "secret-token"],
        },
    )

    payload = response.json()
    errors = payload["error"]["details"]["errors"]
    assert response.status_code == 400
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert "input" not in errors[0]
    assert "secret-token" not in str(payload)


@pytest.mark.integration
def test_request_validation_error_messages_do_not_echo_derived_input_values():
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/v1/exports",
        json={
            "svm": "tenant_a",
            "volume": "vol1",
            "client": "secret-token/24",
        },
    )

    payload = response.json()
    errors = payload["error"]["details"]["errors"]
    assert response.status_code == 400
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert "input" not in errors[0]
    assert "secret-token" not in str(payload)


@pytest.mark.integration
def test_value_error_messages_do_not_echo_derived_query_values():
    client = TestClient(app, raise_server_exceptions=False)

    response = client.delete("/v1/exports?svm=tenant_a&volume=vol1&client=secret-token/24")

    payload = response.json()
    assert response.status_code == 400
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert "secret-token" not in str(payload)


@pytest.mark.integration
def test_arca_error_response_and_log_redact_sensitive_values(caplog):
    client = TestClient(app, raise_server_exceptions=False)
    caplog.set_level(logging.WARNING, logger="arca_storage.api.main")

    with patch(
        "arca_storage.api.services.svm_service.list_svms",
        side_effect=InternalError(
            "backend failed Authorization: Bearer secret-token password=hunter2",
            {"auth_token": "secret-token", "safe": "kept"},
        ),
    ):
        response = client.get("/v1/svms")

    payload = response.json()
    assert response.status_code == 500
    assert payload["error"]["code"] == "INTERNAL"
    assert payload["error"]["details"]["auth_token"] == "<redacted>"
    assert payload["error"]["details"]["safe"] == "kept"
    assert "secret-token" not in str(payload)
    assert "hunter2" not in str(payload)
    assert "secret-token" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "<redacted>" in caplog.text


@pytest.mark.integration
def test_arca_error_log_uses_route_template_not_resource_path(caplog):
    client = TestClient(app, raise_server_exceptions=False)
    caplog.set_level(logging.WARNING, logger="arca_storage.api.main")

    with patch(
        "arca_storage.api.services.volume_service.delete_volume",
        side_effect=InternalError("backend failed"),
    ):
        response = client.delete("/v1/volumes/secret-volume?svm=secret-svm")

    assert response.status_code == 500
    assert "/v1/volumes/{name}" in caplog.text
    assert "secret-volume" not in caplog.text
    assert "secret-svm" not in caplog.text


@pytest.mark.integration
def test_global_exception_log_redacts_sensitive_values(caplog):
    client = TestClient(app, raise_server_exceptions=False)
    caplog.set_level(logging.ERROR, logger="arca_storage.api.main")

    with patch(
        "arca_storage.api.services.svm_service.list_svms",
        side_effect=RuntimeError("Authorization: Bearer secret-token password=hunter2"),
    ):
        response = client.get("/v1/svms")

    payload = response.json()
    assert response.status_code == 500
    assert payload["error"] == {"code": "INTERNAL", "message": "Internal server error", "details": {}}
    assert "secret-token" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "<redacted>" in caplog.text


@pytest.mark.integration
def test_global_exception_log_uses_route_template_not_resource_path(caplog):
    client = TestClient(app, raise_server_exceptions=False)
    caplog.set_level(logging.ERROR, logger="arca_storage.api.main")

    with patch(
        "arca_storage.api.services.volume_service.delete_volume",
        side_effect=RuntimeError("backend failed"),
    ):
        response = client.delete("/v1/volumes/secret-volume?svm=secret-svm")

    assert response.status_code == 500
    assert "/v1/volumes/{name}" in caplog.text
    assert "secret-volume" not in caplog.text
    assert "secret-svm" not in caplog.text


@pytest.mark.integration
@pytest.mark.parametrize(
    "path",
    [
        "/v1/svms?cursor=secret-token",
        "/v1/volumes?cursor=secret-token",
        "/v1/exports?cursor=secret-token",
        "/v1/snapshots?cursor=secret-token",
    ],
)
def test_invalid_cursor_errors_do_not_echo_cursor(fake_context, path: str):
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(path)

    payload = response.json()
    assert response.status_code == 400
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert payload["error"]["details"] == {"field": "cursor"}
    assert "secret-token" not in str(payload)
