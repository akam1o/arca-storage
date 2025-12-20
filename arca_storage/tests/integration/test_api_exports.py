"""
Integration tests for API export endpoints.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from arca_storage.api.main import app


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
            "status": "available",
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
        response = client.post("/v1/exports", json={"svm": "tenant_a", "volume": "vol1", "client": "invalid-cidr"})

        assert response.status_code == 422  # Validation error


class TestRemoveExport:
    """Tests for DELETE /v1/exports."""

    @pytest.mark.integration
    @patch("arca_storage.api.services.export_service.remove_export")
    @pytest.mark.asyncio
    async def test_remove_export_success(self, mock_remove, client):
        """Test successful export removal."""
        mock_remove.return_value = None

        response = client.delete("/v1/exports?svm=tenant_a&volume=vol1&client=10.0.0.0/24")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["data"]["deleted"] is True
