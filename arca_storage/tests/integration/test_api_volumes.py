"""
Integration tests for API volume endpoints.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from arca_storage.api.main import app


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


class TestCreateVolume:
    """Tests for POST /v1/volumes."""

    @pytest.mark.integration
    @patch("arca_storage.api.services.volume_service.create_volume")
    @pytest.mark.asyncio
    async def test_create_volume_success(self, mock_create, client):
        """Test successful volume creation."""
        mock_create.return_value = {
            "name": "vol1",
            "svm": "tenant_a",
            "size_gib": 100,
            "thin": True,
            "fs_type": "xfs",
            "mount_path": "/exports/tenant_a/vol1",
            "lv_path": "/dev/vg_pool_01/vol1",
            "status": "available",
            "created_at": "2025-12-20T12:00:00Z",
        }

        response = client.post(
            "/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 100, "thin": True, "fs_type": "xfs"}
        )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "ok"
        assert "volume" in data["data"]

    @pytest.mark.integration
    def test_create_volume_invalid_name(self, client):
        """Test creating volume with invalid name."""
        response = client.post(
            "/v1/volumes", json={"name": "vol 1", "svm": "tenant_a", "size_gib": 100}  # space in name
        )

        assert response.status_code == 422  # Validation error


class TestResizeVolume:
    """Tests for PATCH /v1/volumes/{name}."""

    @pytest.mark.integration
    @patch("arca_storage.api.services.volume_service.resize_volume")
    @pytest.mark.asyncio
    async def test_resize_volume_success(self, mock_resize, client):
        """Test successful volume resize."""
        mock_resize.return_value = {
            "name": "vol1",
            "svm": "tenant_a",
            "size_gib": 200,
            "thin": True,
            "fs_type": "xfs",
            "mount_path": "/exports/tenant_a/vol1",
            "lv_path": "/dev/vg_pool_01/vol1",
            "status": "available",
            "created_at": "2025-12-20T12:00:00Z",
        }

        response = client.patch("/v1/volumes/vol1", json={"svm": "tenant_a", "new_size_gib": 200})

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["data"]["volume"]["size_gib"] == 200


class TestDeleteVolume:
    """Tests for DELETE /v1/volumes/{name}."""

    @pytest.mark.integration
    @patch("arca_storage.api.services.volume_service.delete_volume")
    @pytest.mark.asyncio
    async def test_delete_volume_success(self, mock_delete, client):
        """Test successful volume deletion."""
        mock_delete.return_value = None

        response = client.delete("/v1/volumes/vol1?svm=tenant_a")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["data"]["deleted"] is True

    @pytest.mark.integration
    def test_delete_volume_removes_exports(self, fake_context):
        """Deleting a volume cleans dependent export records and Ganesha config."""
        client = TestClient(app)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        client.post(
            "/v1/exports",
            json={"svm": "tenant_a", "volume": "vol1", "client": "10.0.0.0/24", "access": "rw"},
        )

        assert fake_context.db.list_exports(svm="tenant_a", volume="vol1")
        assert fake_context.adapters.ganesha.exports["tenant_a"]

        response = client.delete("/v1/volumes/vol1?svm=tenant_a")

        assert response.status_code == 200
        assert fake_context.db.get_volume("tenant_a", "vol1") is None
        assert fake_context.db.list_exports(svm="tenant_a", volume="vol1") == []
        assert fake_context.adapters.ganesha.exports["tenant_a"] == []

    @pytest.mark.integration
    def test_delete_volume_rejects_snapshots_unless_forced(self, fake_context):
        """Snapshots must be explicitly removed or deleted via force."""
        client = TestClient(app)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol1"})

        response = client.delete("/v1/volumes/vol1?svm=tenant_a")

        assert response.status_code == 412
        assert response.json()["error"]["code"] == "PRECONDITION_FAILED"
        assert fake_context.db.get_volume("tenant_a", "vol1") is not None
        assert fake_context.db.list_snapshots(svm="tenant_a", volume="vol1")

        response = client.delete("/v1/volumes/vol1?svm=tenant_a&force=true")

        assert response.status_code == 200
        assert fake_context.db.get_volume("tenant_a", "vol1") is None
        assert fake_context.db.list_snapshots(svm="tenant_a", volume="vol1") == []
