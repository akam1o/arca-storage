"""
Integration tests for API SVM endpoints.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from arca_storage.api.main import app
from arca_storage.errors import NotFoundError


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


class TestCreateSVM:
    """Tests for POST /v1/svms."""

    @pytest.mark.integration
    @patch("arca_storage.api.services.svm_service.create_svm")
    @pytest.mark.asyncio
    async def test_create_svm_success(self, mock_create, client):
        """Test successful SVM creation."""
        mock_create.return_value = {
            "name": "tenant_a",
            "vlan_id": 100,
            "ip_cidr": "192.168.10.5/24",
            "gateway": "192.168.10.1",
            "mtu": 1500,
            "namespace": "tenant_a",
            "vip": "192.168.10.5",
            "status": "available",
            "created_at": "2025-12-20T12:00:00Z",
        }

        response = client.post(
            "/v1/svms",
            json={
                "name": "tenant_a",
                "vlan_id": 100,
                "ip_cidr": "192.168.10.5/24",
                "gateway": "192.168.10.1",
                "mtu": 1500,
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "ok"
        assert "svm" in data["data"]

    @pytest.mark.integration
    def test_create_svm_invalid_name(self, client):
        """Test creating SVM with invalid name."""
        response = client.post(
            "/v1/svms", json={"name": "tenant a", "vlan_id": 100, "ip_cidr": "192.168.10.5/24"}  # space in name
        )

        assert response.status_code == 422  # Validation error

    @pytest.mark.integration
    def test_create_svm_invalid_vlan(self, client):
        """Test creating SVM with invalid VLAN ID."""
        response = client.post(
            "/v1/svms", json={"name": "tenant_a", "vlan_id": 5000, "ip_cidr": "192.168.10.5/24"}  # invalid VLAN ID
        )

        assert response.status_code == 422  # Validation error

    @pytest.mark.integration
    def test_create_svm_invalid_ip(self, client):
        """Test creating SVM with invalid IP."""
        response = client.post(
            "/v1/svms", json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": "invalid-ip"}  # invalid IP
        )

        assert response.status_code == 422  # Validation error

    @pytest.mark.integration
    def test_create_svm_without_vlan(self, fake_context):
        """Test creating an SVM without a VLAN ID."""
        client = TestClient(app)
        response = client.post(
            "/v1/svms",
            json={"name": "tenant_a", "ip_cidr": "192.168.10.5/32"},
        )

        assert response.status_code == 201
        data = response.json()["data"]["svm"]
        assert data["vlan_id"] is None
        assert data["vip"] == "192.168.10.5"
        assert fake_context.adapters.netns.namespace_exists("tenant_a") is False
        assert fake_context.adapters.ganesha.host_network["tenant_a"] is True


class TestListSVMs:
    """Tests for GET /v1/svms."""

    @pytest.mark.integration
    @patch("arca_storage.api.services.svm_service.list_svms")
    @pytest.mark.asyncio
    async def test_list_svms_success(self, mock_list, client):
        """Test successful SVM listing."""
        mock_list.return_value = {"items": [], "next_cursor": None}

        response = client.get("/v1/svms")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "items" in data["data"]

    @pytest.mark.integration
    @patch("arca_storage.api.services.svm_service.list_svms")
    @pytest.mark.asyncio
    async def test_list_svms_with_filter(self, mock_list, client):
        """Test listing SVMs with name filter."""
        mock_list.return_value = {"items": [], "next_cursor": None}

        response = client.get("/v1/svms?name=tenant_a")

        assert response.status_code == 200
        mock_list.assert_called_once()


class TestDeleteSVM:
    """Tests for DELETE /v1/svms/{name}."""

    @pytest.mark.integration
    @patch("arca_storage.api.services.svm_service.delete_svm")
    @pytest.mark.asyncio
    async def test_delete_svm_success(self, mock_delete, client):
        """Test successful SVM deletion."""
        mock_delete.return_value = None

        response = client.delete("/v1/svms/tenant_a")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["data"]["deleted"] is True

    @pytest.mark.integration
    @patch("arca_storage.api.services.svm_service.delete_svm")
    @pytest.mark.asyncio
    async def test_delete_svm_not_found(self, mock_delete, client):
        """Test deleting non-existent SVM."""
        mock_delete.side_effect = NotFoundError("SVM", "nonexistent")

        response = client.delete("/v1/svms/nonexistent")

        assert response.status_code == 404

    @pytest.mark.integration
    def test_delete_svm_rejects_existing_volumes(self, fake_context):
        """SVM deletion refuses to orphan dependent volumes by default."""
        client = TestClient(app)
        client.post(
            "/v1/svms",
            json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": "192.168.10.5/24", "gateway": "192.168.10.1"},
        )
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})

        response = client.delete("/v1/svms/tenant_a")

        assert response.status_code == 412
        assert response.json()["error"]["code"] == "PRECONDITION_FAILED"
        assert fake_context.db.get_svm("tenant_a") is not None
        assert fake_context.db.get_volume("tenant_a", "vol1") is not None

    @pytest.mark.integration
    def test_delete_svm_delete_volumes_cascades(self, fake_context):
        """delete_volumes removes dependent volumes before deleting the SVM."""
        client = TestClient(app)
        client.post(
            "/v1/svms",
            json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": "192.168.10.5/24", "gateway": "192.168.10.1"},
        )
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})

        response = client.delete("/v1/svms/tenant_a?delete_volumes=true")

        assert response.status_code == 200
        assert fake_context.db.get_svm("tenant_a") is None
        assert fake_context.db.get_volume("tenant_a", "vol1") is None

    @pytest.mark.integration
    def test_delete_svm_force_cascades_snapshots(self, fake_context):
        """force cascades through snapshots without leaving DB state behind."""
        client = TestClient(app)
        client.post(
            "/v1/svms",
            json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": "192.168.10.5/24", "gateway": "192.168.10.1"},
        )
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol1"})

        response = client.delete("/v1/svms/tenant_a?delete_volumes=true")

        assert response.status_code == 412
        assert fake_context.db.get_svm("tenant_a") is not None

        response = client.delete("/v1/svms/tenant_a?force=true")

        assert response.status_code == 200
        assert fake_context.db.get_svm("tenant_a") is None
        assert fake_context.db.get_volume("tenant_a", "vol1") is None
        assert fake_context.db.list_snapshots(svm="tenant_a", volume="vol1") == []
