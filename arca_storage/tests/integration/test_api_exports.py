"""
Integration tests for API export endpoints.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from arca_storage.api.main import app
from arca_storage.api.services import export_service


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


class TestAddExport:
    """Tests for POST /v1/exports."""

    @pytest.mark.integration
    @patch("arca_storage.api.services.export_service.add_export")
    @pytest.mark.asyncio
    async def test_add_export_success(self, mock_add, client):
        """Test successful export addition."""
        mock_add.return_value = {
            "svm": "tenant_a",
            "volume": "vol1",
            "client": "10.0.0.0/24",
            "access": "rw",
            "root_squash": True,
            "sec": ["sys"],
            "pseudo": "/exports/tenant_a/vol1",
            "export_id": 101,
            "status": "Ready",
            "created_at": "2025-12-20T12:00:00Z",
        }

        response = client.post(
            "/v1/exports",
            json={
                "svm": "tenant_a",
                "volume": "vol1",
                "client": "10.0.0.0/24",
                "access": "rw",
                "root_squash": True,
                "sec": ["sys"],
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "ok"
        assert "export" in data["data"]

    @pytest.mark.integration
    def test_add_export_invalid_client(self, client):
        """Test adding export with invalid client CIDR."""
        response = client.post(
            "/v1/exports",
            json={"svm": "tenant_a", "volume": "vol1", "client": "invalid-cidr"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    @pytest.mark.integration
    def test_add_export_rejects_default_route_client(self, client):
        """Test adding export refuses world-open client CIDRs."""
        response = client.post(
            "/v1/exports",
            json={"svm": "tenant_a", "volume": "vol1", "client": "0.0.0.0/0"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
        assert (
            "default route" in response.json()["error"]["details"]["errors"][0]["msg"]
        )

    @pytest.mark.integration
    def test_add_export_rejects_unsupported_sec_type(self, client):
        response = client.post(
            "/v1/exports",
            json={
                "svm": "tenant_a",
                "volume": "vol1",
                "client": "10.0.0.0/24",
                "sec": ["sys", "bad; token"],
            },
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    @pytest.mark.integration
    def test_add_export_rolls_back_rendered_config_on_reload_failure(
        self, fake_context
    ):
        client = TestClient(app, raise_server_exceptions=False)
        client.post(
            "/v1/svms",
            json={
                "name": "tenant_a",
                "vlan_id": 100,
                "ip_cidr": "192.168.10.5/24",
                "gateway": "192.168.10.1",
            },
        )
        client.post(
            "/v1/volumes", json={"name": "ready", "svm": "tenant_a", "size_gib": 10}
        )
        client.post(
            "/v1/exports",
            json={
                "svm": "tenant_a",
                "volume": "ready",
                "client": "10.0.0.0/24",
                "access": "rw",
            },
        )
        client.post(
            "/v1/volumes", json={"name": "failed", "svm": "tenant_a", "size_gib": 10}
        )

        def fail_reload(_svm, *, host_network=False):
            raise RuntimeError("reload failed")

        fake_context.adapters.ganesha.reload = fail_reload

        response = client.post(
            "/v1/exports",
            json={
                "svm": "tenant_a",
                "volume": "failed",
                "client": "10.0.1.0/24",
                "access": "rw",
            },
        )

        assert response.status_code == 500
        assert [
            entry["path"] for entry in fake_context.adapters.ganesha.exports["tenant_a"]
        ] == ["/exports/tenant_a/ready"]

    @pytest.mark.integration
    def test_export_client_cidr_is_canonical_for_matching(self, fake_context):
        client = TestClient(app, raise_server_exceptions=False)
        client.post(
            "/v1/svms",
            json={
                "name": "tenant_a",
                "vlan_id": 100,
                "ip_cidr": "192.168.10.5/24",
                "gateway": "192.168.10.1",
            },
        )
        client.post(
            "/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10}
        )

        response = client.post(
            "/v1/exports",
            json={
                "svm": "tenant_a",
                "volume": "vol1",
                "client": "10.0.0.1/24",
                "access": "rw",
            },
        )
        assert response.status_code == 201
        assert response.json()["data"]["export"]["client"] == "10.0.0.0/24"
        assert fake_context.db.get_export("tenant_a", "vol1", "10.0.0.0/24") is not None

        response = client.post(
            "/v1/exports",
            json={
                "svm": "tenant_a",
                "volume": "vol1",
                "client": "10.0.0.0/24",
                "access": "rw",
            },
        )
        assert response.status_code == 409

        response = client.get(
            "/v1/exports?svm=tenant_a&volume=vol1&client=10.0.0.99/24"
        )
        assert response.status_code == 200
        assert [item["client"] for item in response.json()["data"]["items"]] == [
            "10.0.0.0/24"
        ]

        response = client.delete(
            "/v1/exports?svm=tenant_a&volume=vol1&client=10.0.0.99/24"
        )
        assert response.status_code == 200
        assert fake_context.db.get_export("tenant_a", "vol1", "10.0.0.0/24") is None

    @pytest.mark.integration
    def test_add_export_returns_conflict_when_existing_export_lease_is_not_acquired(
        self, fake_context, monkeypatch
    ):
        client = TestClient(app, raise_server_exceptions=False)
        client.post(
            "/v1/svms",
            json={
                "name": "tenant_a",
                "vlan_id": 100,
                "ip_cidr": "192.168.10.5/24",
                "gateway": "192.168.10.1",
            },
        )
        client.post(
            "/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10}
        )
        client.post(
            "/v1/exports",
            json={
                "svm": "tenant_a",
                "volume": "vol1",
                "client": "10.0.0.0/24",
                "access": "rw",
            },
        )

        original_can_resume = export_service._can_resume_create

        def can_resume_create(record, requested_spec, *, owner=None):
            assert record is not None
            return original_can_resume(record, requested_spec, owner=owner)

        monkeypatch.setattr(export_service, "_can_resume_create", can_resume_create)
        monkeypatch.setattr(
            fake_context.db,
            "acquire_export_create_lease",
            lambda *_args, **_kwargs: None,
        )

        response = client.post(
            "/v1/exports",
            json={
                "svm": "tenant_a",
                "volume": "vol1",
                "client": "10.0.0.0/24",
                "access": "rw",
            },
        )

        assert response.status_code == 409

    @pytest.mark.integration
    def test_add_export_rejects_unready_volume(self, fake_context):
        from arca_storage.models.volume import Volume, VolumeSpec

        client = TestClient(app)
        client.post(
            "/v1/svms",
            json={
                "name": "tenant_a",
                "vlan_id": 100,
                "ip_cidr": "192.168.10.5/24",
                "gateway": "192.168.10.1",
            },
        )
        fake_context.db.insert_volume(
            Volume(spec=VolumeSpec(name="vol1", svm="tenant_a", size_gib=10))
        )

        response = client.post(
            "/v1/exports",
            json={
                "svm": "tenant_a",
                "volume": "vol1",
                "client": "10.0.0.0/24",
                "access": "rw",
            },
        )

        assert response.status_code == 412
        assert response.json()["error"]["code"] == "PRECONDITION_FAILED"
        assert response.json()["error"]["details"]["phase"] == "Pending"
        assert fake_context.db.list_exports(svm="tenant_a", volume="vol1") == []
        assert fake_context.adapters.ganesha.exports.get("tenant_a", []) == []


class TestRemoveExport:
    """Tests for DELETE /v1/exports."""

    @pytest.mark.integration
    @patch("arca_storage.api.services.export_service.remove_export")
    @pytest.mark.asyncio
    async def test_remove_export_success(self, mock_remove, client):
        """Test successful export removal."""
        mock_remove.return_value = None

        response = client.delete(
            "/v1/exports?svm=tenant_a&volume=vol1&client=10.0.0.0/24"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["data"]["deleted"] is True

    @pytest.mark.integration
    def test_remove_export_reports_reconciler_failure(self, fake_context):
        client = TestClient(app)
        client.post(
            "/v1/svms",
            json={
                "name": "tenant_a",
                "vlan_id": 100,
                "ip_cidr": "192.168.10.5/24",
                "gateway": "192.168.10.1",
            },
        )
        client.post(
            "/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10}
        )
        client.post(
            "/v1/exports",
            json={
                "svm": "tenant_a",
                "volume": "vol1",
                "client": "10.0.0.0/24",
                "access": "rw",
            },
        )

        def fail_reload(_svm, *, host_network=False):
            raise RuntimeError("reload failed")

        fake_context.adapters.ganesha.reload = fail_reload

        response = client.delete(
            "/v1/exports?svm=tenant_a&volume=vol1&client=10.0.0.0/24"
        )

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL"
        record = fake_context.db.get_export("tenant_a", "vol1", "10.0.0.0/24")
        assert record["status"]["phase"] == "Failed"
        assert record["status"]["message"].startswith("Delete failed:")
        assert [
            entry["path"] for entry in fake_context.adapters.ganesha.exports["tenant_a"]
        ] == ["/exports/tenant_a/vol1"]


class TestListExports:
    """Tests for GET /v1/exports."""

    @pytest.mark.integration
    def test_list_exports_returns_public_shape(self, fake_context):
        client = TestClient(app)
        client.post(
            "/v1/svms",
            json={
                "name": "tenant_a",
                "vlan_id": 100,
                "ip_cidr": "192.168.10.5/24",
                "gateway": "192.168.10.1",
            },
        )
        client.post(
            "/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10}
        )
        client.post(
            "/v1/exports",
            json={
                "svm": "tenant_a",
                "volume": "vol1",
                "client": "10.0.0.0/24",
                "access": "rw",
            },
        )

        response = client.get("/v1/exports?svm=tenant_a&volume=vol1")

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["svm"] == "tenant_a"
        assert item["volume"] == "vol1"
        assert item["client"] == "10.0.0.0/24"
        assert item["access"] == "rw"
        assert item["export_id"] == 1
        assert "spec" not in item
        assert "status" in item
