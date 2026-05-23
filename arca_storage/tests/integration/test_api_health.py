"""Integration tests for API health and monitoring endpoints."""

import re

from fastapi.testclient import TestClient

from arca_storage.api.main import app


def test_healthz_returns_liveness_payload():
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["data"] == {"state": "live"}
    assert payload["request_id"]


def test_readyz_checks_database(fake_context):
    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["data"]["checks"] == {"db": "ok"}


def test_readyz_reports_database_failure(fake_context, monkeypatch):
    def fail_list_svms(*_args, **_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(fake_context.db, "list_svms", fail_list_svms)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["error"] == {
        "code": "UNAVAILABLE",
        "message": "Readiness check failed",
        "details": {"checks": {"db": "error"}},
    }


def test_metrics_returns_prometheus_text():
    with TestClient(app) as client:
        client.get("/healthz")
        client.get("/missing-metrics-probe")
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "arca_storage_api_up 1" in response.text
    assert "arca_storage_http_requests_total" in response.text
    assert "arca_storage_http_request_failures_total" in response.text
    assert "arca_storage_http_request_latency_seconds_count" in response.text
    assert re.search(
        r'arca_storage_http_requests_total\{method="GET",route="/healthz",status="200"\} [1-9]\d*',
        response.text,
    )
    assert re.search(
        r'arca_storage_http_request_failures_total\{method="GET",route="/<unmatched>",status="404"\} [1-9]\d*',
        response.text,
    )
