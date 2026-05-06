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


def create_test_svm(client: TestClient) -> None:
    client.post(
        "/v1/svms",
        json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": "192.168.10.5/24", "gateway": "192.168.10.1"},
    )


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

    @pytest.mark.integration
    def test_create_volume_requires_existing_svm(self, fake_context):
        client = TestClient(app)

        response = client.post("/v1/volumes", json={"name": "vol1", "svm": "missing", "size_gib": 10})

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    @pytest.mark.integration
    def test_create_volume_rejects_duplicate_without_mutating_existing(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        first = client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        assert first.status_code == 201
        assert first.json()["data"]["volume"]["export_path"] == "192.168.10.5:/exports/tenant_a/vol1"

        response = client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 20})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ALREADY_EXISTS"
        record = fake_context.db.get_volume("tenant_a", "vol1")
        assert record["spec"]["size_gib"] == 10
        assert record["status"]["phase"] == "Ready"


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

    @pytest.mark.integration
    def test_resize_volume_requires_existing_record_before_mutation(self, fake_context):
        client = TestClient(app)
        fake_context.adapters.lvm.create_thin_lv("vg_pool_01", "pool", "vol_tenant_a_missing", 10)
        fake_context.adapters.xfs.mount("/dev/vg_pool_01/vol_tenant_a_missing", "/exports/tenant_a/missing")

        response = client.patch("/v1/volumes/missing", json={"svm": "tenant_a", "new_size_gib": 20})

        assert response.status_code == 404
        assert fake_context.adapters.lvm.volumes["vg_pool_01/vol_tenant_a_missing"] == 10

    @pytest.mark.integration
    def test_resize_volume_rejects_shrink_without_mutation(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 20})

        response = client.patch("/v1/volumes/vol1", json={"svm": "tenant_a", "new_size_gib": 10})

        assert response.status_code == 412
        assert fake_context.db.get_volume("tenant_a", "vol1")["spec"]["size_gib"] == 20
        assert fake_context.adapters.lvm.volumes["vg_pool_01/vol_tenant_a_vol1"] == 20


class TestCloneVolume:
    """Tests for POST /v1/volumes/{name}/clone."""

    @pytest.mark.integration
    def test_clone_volume_applies_larger_requested_size(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol1"})

        response = client.post(
            "/v1/volumes/vol1/clone",
            json={"name": "clone1", "svm": "tenant_a", "snapshot": "snap1", "size_gib": 20},
        )

        assert response.status_code == 201
        volume = response.json()["data"]["volume"]
        assert volume["size_gib"] == 20
        assert volume["export_path"] == "192.168.10.5:/exports/tenant_a/clone1"
        assert fake_context.adapters.lvm.volumes["vg_pool_01/vol_tenant_a_clone1"] == 20
        assert fake_context.db.get_volume("tenant_a", "clone1")["spec"]["size_gib"] == 20
        assert fake_context.adapters.xfs.mount_options["/exports/tenant_a/clone1"] == ["nouuid"]

    @pytest.mark.integration
    def test_clone_volume_uses_source_volume_from_route(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        client.post("/v1/volumes", json={"name": "vol2", "svm": "tenant_a", "size_gib": 30})
        client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol1"})
        client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol2"})

        response = client.post(
            "/v1/volumes/vol2/clone",
            json={"name": "clone2", "svm": "tenant_a", "snapshot": "snap1"},
        )

        assert response.status_code == 201
        volume = response.json()["data"]["volume"]
        assert volume["size_gib"] == 30
        assert fake_context.adapters.lvm.volumes["vg_pool_01/vol_tenant_a_clone2"] == 30

    @pytest.mark.integration
    def test_clone_volume_cleans_up_new_lv_on_mount_or_grow_failure(self, fake_context):
        client = TestClient(app, raise_server_exceptions=False)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol1"})

        def fail_grow(_mount_path):
            raise RuntimeError("grow failed")

        fake_context.adapters.xfs.grow = fail_grow

        response = client.post(
            "/v1/volumes/vol1/clone",
            json={"name": "clone1", "svm": "tenant_a", "snapshot": "snap1", "size_gib": 20},
        )

        assert response.status_code == 500
        assert not fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_clone1")
        assert "/exports/tenant_a/clone1" not in fake_context.adapters.xfs.mounts
        assert fake_context.db.get_volume("tenant_a", "clone1") is None


class TestSnapshots:
    """Tests for API snapshot behavior."""

    @pytest.mark.integration
    def test_create_snapshot_rejects_thick_volume(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        response = client.post(
            "/v1/volumes",
            json={"name": "vol1", "svm": "tenant_a", "size_gib": 10, "thin": False},
        )
        assert response.status_code == 201

        response = client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol1"})

        assert response.status_code == 412
        assert response.json()["error"]["code"] == "PRECONDITION_FAILED"
        assert fake_context.db.list_snapshots(svm="tenant_a", volume="vol1") == []

    @pytest.mark.integration
    def test_delete_snapshot_reports_reconciler_failure(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol1"})

        def fail_delete(_vg, _lv):
            raise RuntimeError("snapshot delete failed")

        fake_context.adapters.lvm.delete_lv = fail_delete

        response = client.delete("/v1/snapshots/snap1?svm=tenant_a&volume=vol1")

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL"
        record = fake_context.db.list_snapshots(svm="tenant_a", volume="vol1", name="snap1")[0]
        assert record["status"]["phase"] == "Failed"
        assert record["status"]["message"].startswith("Delete failed:")


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
        create_test_svm(client)
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
        create_test_svm(client)
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

    @pytest.mark.integration
    def test_create_volume_does_not_resume_failed_delete(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})

        def fail_delete(_vg, _lv):
            raise RuntimeError("delete failed")

        fake_context.adapters.lvm.delete_lv = fail_delete

        delete_response = client.delete("/v1/volumes/vol1?svm=tenant_a")
        assert delete_response.status_code == 500
        assert delete_response.json()["error"]["code"] == "INTERNAL"
        failed_record = fake_context.db.get_volume("tenant_a", "vol1")
        assert failed_record["status"]["phase"] == "Failed"
        assert failed_record["status"]["message"].startswith("Delete failed:")
        assert "/exports/tenant_a/vol1" not in fake_context.adapters.xfs.mounts

        create_response = client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})

        assert create_response.status_code == 409
        assert fake_context.db.get_volume("tenant_a", "vol1")["status"]["phase"] == "Failed"
