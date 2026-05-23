"""
Integration tests for API validation errors raised below request parsing.
"""

import pytest
from fastapi.testclient import TestClient

from arca_storage.api.main import app


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
