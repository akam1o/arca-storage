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
