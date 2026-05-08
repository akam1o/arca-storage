"""
Integration tests for API SVM endpoints.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from arca_storage.api.main import app
from arca_storage.api.services import export_service
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
            "status": "Ready",
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

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    @pytest.mark.integration
    def test_create_svm_invalid_vlan(self, client):
        """Test creating SVM with invalid VLAN ID."""
        response = client.post(
            "/v1/svms", json={"name": "tenant_a", "vlan_id": 5000, "ip_cidr": "192.168.10.5/24"}  # invalid VLAN ID
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    @pytest.mark.integration
    def test_create_svm_invalid_ip(self, client):
        """Test creating SVM with invalid IP."""
        response = client.post(
            "/v1/svms", json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": "invalid-ip"}  # invalid IP
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "ip_cidr", ["0.0.0.0/0", "10.0.0.1/0", "192.168.10.0/24", "192.168.10.255/24", "224.0.0.1/24"]
    )
    def test_create_svm_rejects_non_host_vip(self, client, ip_cidr):
        """Test creating SVM rejects non-host VIP addresses."""
        response = client.post("/v1/svms", json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": ip_cidr})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    @pytest.mark.integration
    @pytest.mark.parametrize("gateway", ["10.0.0.1", "192.168.10.5", "192.168.10.0", "192.168.10.255"])
    def test_create_svm_rejects_unusable_gateway(self, client, fake_context, gateway):
        """Test creating SVM rejects gateways that cannot route from the SVM VIP."""
        response = client.post(
            "/v1/svms",
            json={
                "name": "tenant_a",
                "vlan_id": 100,
                "ip_cidr": "192.168.10.5/24",
                "gateway": gateway,
            },
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
        assert fake_context.db.get_svm("tenant_a") is None

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
        assert data["export_root"] == "/exports/tenant_a"
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

    @pytest.mark.integration
    def test_list_svms_rejects_invalid_cursor_with_filter(self, fake_context):
        client = TestClient(app)

        response = client.get("/v1/svms?name=tenant_a&cursor=not-a-cursor")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


class TestSVMCapacity:
    """Tests for GET /v1/svms/{name}/capacity."""

    @pytest.mark.integration
    def test_get_svm_capacity(self, fake_context):
        """Capacity endpoint returns scheduler-friendly numeric values."""
        client = TestClient(app)
        client.post(
            "/v1/svms",
            json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": "192.168.10.5/24", "gateway": "192.168.10.1"},
        )
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})

        response = client.get("/v1/svms/tenant_a/capacity")

        assert response.status_code == 200
        capacity = response.json()["data"]["capacity"]
        assert capacity["svm"] == "tenant_a"
        assert capacity["total_gb"] >= capacity["free_gb"]
        assert capacity["provisioned_gb"] == 10.0


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
        assert fake_context.db.get_svm("tenant_a")["status"]["phase"] == "Ready"
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
    def test_delete_svm_removes_root_volume(self, fake_context):
        """Deleting an SVM with a root volume frees the backing LV."""
        client = TestClient(app)
        response = client.post(
            "/v1/svms",
            json={
                "name": "tenant_a",
                "vlan_id": 100,
                "ip_cidr": "192.168.10.5/24",
                "gateway": "192.168.10.1",
                "root_volume_size_gib": 10,
            },
        )
        assert response.status_code == 201
        assert fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a")

        response = client.delete("/v1/svms/tenant_a")

        assert response.status_code == 200
        assert not fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a")

    @pytest.mark.integration
    def test_delete_svm_reports_reconciler_failure(self, fake_context):
        client = TestClient(app)
        client.post(
            "/v1/svms",
            json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": "192.168.10.5/24", "gateway": "192.168.10.1"},
        )

        def fail_delete(_name):
            record = fake_context.db.get_svm("tenant_a")
            assert record["status"]["phase"] == "Deleting"
            raise RuntimeError("pacemaker delete failed")

        fake_context.adapters.pacemaker.delete_group = fail_delete

        response = client.delete("/v1/svms/tenant_a")

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL"
        record = fake_context.db.get_svm("tenant_a")
        assert record["status"]["phase"] == "Failed"
        assert record["status"]["message"].startswith("Delete failed:")

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

    @pytest.mark.integration
    def test_delete_svm_force_removes_internal_root_export(self, fake_context):
        """force removes CSI root exports that do not pass public volume validation."""
        client = TestClient(app)
        client.post(
            "/v1/svms",
            json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": "192.168.10.5/24", "gateway": "192.168.10.1"},
        )
        export_service.ensure_internal_export(
            "tenant_a",
            "__csi_root__",
            "10.0.0.0/24",
            path="/exports/tenant_a",
            pseudo="/exports/tenant_a",
            owner="csi",
        )
        assert fake_context.db.get_export("tenant_a", "__csi_root__", "10.0.0.0/24") is not None

        response = client.delete("/v1/svms/tenant_a?force=true")

        assert response.status_code == 200
        assert fake_context.db.get_svm("tenant_a") is None
        assert fake_context.db.list_exports(svm="tenant_a") == []
