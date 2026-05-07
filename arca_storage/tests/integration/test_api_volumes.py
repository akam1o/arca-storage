"""
Integration tests for API volume endpoints.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from arca_storage.api.main import app
from arca_storage.create_resume import assign_create_lease


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


def create_test_svm(client: TestClient) -> None:
    client.post(
        "/v1/svms",
        json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": "192.168.10.5/24", "gateway": "192.168.10.1"},
    )


def switch_export_dir(fake_context, export_dir: str) -> None:
    from arca_storage.reconcilers.volume import VolumeReconciler

    cfg = {**fake_context.settings.to_reconciler_config(), "export_dir": export_dir}
    fake_context.settings.to_reconciler_config = lambda: cfg
    fake_context.volume_reconciler = VolumeReconciler(fake_context.db, fake_context.adapters, config=cfg)


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
            "status": "Ready",
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

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    @pytest.mark.integration
    def test_create_volume_rejects_unsupported_fs_type(self, client):
        response = client.post(
            "/v1/volumes",
            json={"name": "vol1", "svm": "tenant_a", "size_gib": 100, "fs_type": "ext4"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    @pytest.mark.integration
    def test_create_volume_requires_existing_svm(self, fake_context):
        client = TestClient(app)

        response = client.post("/v1/volumes", json={"name": "vol1", "svm": "missing", "size_gib": 10})

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    @pytest.mark.integration
    def test_create_volume_rejects_unready_svm(self, fake_context):
        from arca_storage.models.svm import SVM, SVMSpec

        client = TestClient(app)
        fake_context.db.insert_svm(
            SVM(spec=SVMSpec(name="tenant_a", vlan_id=100, ip_cidr="192.168.10.5/24", gateway="192.168.10.1"))
        )

        response = client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})

        assert response.status_code == 412
        assert response.json()["error"]["code"] == "PRECONDITION_FAILED"
        assert response.json()["error"]["details"] == {
            "resource": "SVM",
            "name": "tenant_a",
            "phase": "Pending",
        }
        assert fake_context.db.get_volume("tenant_a", "vol1") is None

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

    @pytest.mark.integration
    def test_create_volume_rejects_reserved_duplicate_without_side_effects(self, fake_context):
        from arca_storage.models.volume import Volume, VolumeSpec

        client = TestClient(app)
        create_test_svm(client)
        fake_context.db.insert_volume(Volume(spec=VolumeSpec(name="vol1", svm="tenant_a", size_gib=10)))

        response = client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})

        assert response.status_code == 409
        assert not fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_vol1")

    @pytest.mark.integration
    def test_create_volume_rejects_live_leased_duplicate_without_side_effects(self, fake_context):
        from arca_storage.models.volume import Volume, VolumeSpec

        client = TestClient(app)
        create_test_svm(client)
        volume = Volume(spec=VolumeSpec(name="vol1", svm="tenant_a", size_gib=10))
        assign_create_lease(volume.status, "live-owner")
        fake_context.db.insert_volume(volume)

        response = client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})

        assert response.status_code == 409
        assert not fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_vol1")

    @pytest.mark.integration
    def test_create_volume_resumes_stale_reserved_duplicate(self, fake_context):
        from arca_storage.models.volume import Volume, VolumeSpec

        client = TestClient(app)
        create_test_svm(client)
        fake_context.db.insert_volume(Volume(spec=VolumeSpec(name="vol1", svm="tenant_a", size_gib=10)))
        expired_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        record = fake_context.db.get_volume("tenant_a", "vol1")
        status = record["status"]
        status["phase"] = "Creating"
        status["create_owner"] = "dead-owner"
        status["create_lease_expires_at"] = expired_at
        conn = fake_context.db._conn()
        conn.execute(
            "UPDATE volumes SET status = ? WHERE svm = ? AND name = ?",
            (json.dumps(status), "tenant_a", "vol1"),
        )
        conn.commit()

        response = client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})

        assert response.status_code == 201
        assert response.json()["data"]["volume"]["status"] == "Ready"
        assert fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_vol1")


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
            "status": "Ready",
            "created_at": "2025-12-20T12:00:00Z",
        }

        response = client.patch("/v1/volumes/vol1", json={"svm": "tenant_a", "new_size_gib": 200})

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["data"]["volume"]["size_gib"] == 200

    @pytest.mark.integration
    def test_resize_volume_preserves_created_at_in_response(self, fake_context):
        from arca_storage.models.base import Phase, ResourceMeta
        from arca_storage.models.volume import Volume, VolumeSpec, VolumeStatus

        client = TestClient(app)
        create_test_svm(client)
        created_at = datetime(2025, 12, 20, 12, 0, tzinfo=timezone.utc)
        fake_context.db.insert_volume(
            Volume(
                metadata=ResourceMeta(
                    id="volume-a",
                    generation=3,
                    created_at=created_at,
                    updated_at=created_at,
                ),
                spec=VolumeSpec(name="vol1", svm="tenant_a", size_gib=20),
                status=VolumeStatus(
                    phase=Phase.READY,
                    lv_created=True,
                    lv_path="/dev/vg_pool_01/vol_tenant_a_vol1",
                    lv_name="vol_tenant_a_vol1",
                    fs_formatted=True,
                    mounted=True,
                    mount_path="/exports/tenant_a/vol1",
                ),
            )
        )
        fake_context.adapters.lvm.create_thin_lv("vg_pool_01", "pool", "vol_tenant_a_vol1", 20)
        fake_context.adapters.xfs.mount("/dev/vg_pool_01/vol_tenant_a_vol1", "/exports/tenant_a/vol1")

        response = client.patch("/v1/volumes/vol1", json={"svm": "tenant_a", "new_size_gib": 40})

        assert response.status_code == 200
        returned_created_at = response.json()["data"]["volume"]["created_at"].replace("Z", "+00:00")
        assert datetime.fromisoformat(returned_created_at) == created_at

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

    @pytest.mark.integration
    def test_resize_volume_rejects_unready_record_without_mutation(self, fake_context):
        from arca_storage.models.volume import Volume, VolumeSpec

        client = TestClient(app)
        create_test_svm(client)
        fake_context.db.insert_volume(Volume(spec=VolumeSpec(name="vol1", svm="tenant_a", size_gib=20)))

        response = client.patch("/v1/volumes/vol1", json={"svm": "tenant_a", "new_size_gib": 40})

        assert response.status_code == 412
        assert response.json()["error"]["code"] == "PRECONDITION_FAILED"
        assert response.json()["error"]["details"]["phase"] == "Pending"
        assert fake_context.db.get_volume("tenant_a", "vol1")["spec"]["size_gib"] == 20
        assert not fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_vol1")

    @pytest.mark.integration
    def test_resize_volume_retries_grow_after_lv_extension(self, fake_context):
        client = TestClient(app, raise_server_exceptions=False)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 20})

        original_grow = fake_context.adapters.xfs.grow
        grow_calls = {"count": 0}

        def flaky_grow(mount_path):
            grow_calls["count"] += 1
            if grow_calls["count"] == 1:
                raise RuntimeError("xfs_growfs failed")
            original_grow(mount_path)

        fake_context.adapters.xfs.grow = flaky_grow

        response = client.patch("/v1/volumes/vol1", json={"svm": "tenant_a", "new_size_gib": 40})
        assert response.status_code == 500
        assert fake_context.adapters.lvm.volumes["vg_pool_01/vol_tenant_a_vol1"] == 40
        assert fake_context.db.get_volume("tenant_a", "vol1")["spec"]["size_gib"] == 20

        response = client.patch("/v1/volumes/vol1", json={"svm": "tenant_a", "new_size_gib": 30})
        assert response.status_code == 412
        assert fake_context.adapters.lvm.volumes["vg_pool_01/vol_tenant_a_vol1"] == 40
        assert fake_context.db.get_volume("tenant_a", "vol1")["spec"]["size_gib"] == 20
        assert grow_calls["count"] == 1

        response = client.patch("/v1/volumes/vol1", json={"svm": "tenant_a", "new_size_gib": 40})

        assert response.status_code == 200
        assert response.json()["data"]["volume"]["size_gib"] == 40
        assert grow_calls["count"] == 2

    @pytest.mark.integration
    def test_resize_volume_uses_persisted_mount_path_after_export_dir_change(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        switch_export_dir(fake_context, "/newexports")

        response = client.patch("/v1/volumes/vol1", json={"svm": "tenant_a", "new_size_gib": 20})

        assert response.status_code == 200
        assert fake_context.adapters.lvm.volumes["vg_pool_01/vol_tenant_a_vol1"] == 20
        assert fake_context.db.get_volume("tenant_a", "vol1")["spec"]["size_gib"] == 20


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
        assert volume["thin"] is True
        assert volume["fs_type"] == "xfs"
        assert volume["lv_name"] == "vol_tenant_a_clone1"
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
    def test_clone_volume_uses_snapshot_size_after_source_expansion(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol1"})
        client.patch("/v1/volumes/vol1", json={"svm": "tenant_a", "new_size_gib": 20})

        response = client.post(
            "/v1/volumes/vol1/clone",
            json={"name": "clone1", "svm": "tenant_a", "snapshot": "snap1"},
        )

        assert response.status_code == 201
        volume = response.json()["data"]["volume"]
        assert volume["size_gib"] == 10
        assert fake_context.adapters.lvm.volumes["vg_pool_01/vol_tenant_a_clone1"] == 10
        assert fake_context.db.get_volume("tenant_a", "clone1")["spec"]["size_gib"] == 10

    @pytest.mark.integration
    def test_clone_volume_resume_uses_persisted_mount_path_after_export_dir_change(self, fake_context):
        from arca_storage.models.volume import Volume, VolumeSpec

        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol1"})

        clone = Volume(spec=VolumeSpec(name="clone1", svm="tenant_a", size_gib=10, thin=True))
        assign_create_lease(clone.status, "dead-owner")
        clone.status.lv_created = True
        clone.status.lv_path = "/dev/vg_pool_01/vol_tenant_a_clone1"
        clone.status.lv_name = "vol_tenant_a_clone1"
        clone.status.fs_formatted = True
        clone.status.mounted = True
        clone.status.mount_path = "/exports/tenant_a/clone1"
        fake_context.db.insert_volume(clone)
        expired_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        record = fake_context.db.get_volume("tenant_a", "clone1")
        status = record["status"]
        status["create_lease_expires_at"] = expired_at
        conn = fake_context.db._conn()
        conn.execute(
            "UPDATE volumes SET status = ? WHERE svm = ? AND name = ?",
            (json.dumps(status), "tenant_a", "clone1"),
        )
        conn.commit()
        fake_context.adapters.lvm.create_snapshot(
            "vg_pool_01",
            "vol_tenant_a_vol1_snap_snap1",
            "vol_tenant_a_clone1",
        )
        fake_context.adapters.xfs.mount(
            "/dev/vg_pool_01/vol_tenant_a_clone1",
            "/exports/tenant_a/clone1",
            extra_options=["nouuid"],
        )
        switch_export_dir(fake_context, "/newexports")

        response = client.post(
            "/v1/volumes/vol1/clone",
            json={"name": "clone1", "svm": "tenant_a", "snapshot": "snap1"},
        )

        assert response.status_code == 201
        volume = response.json()["data"]["volume"]
        assert volume["mount_path"] == "/exports/tenant_a/clone1"
        assert "/newexports/tenant_a/clone1" not in fake_context.adapters.xfs.mounts

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
        record = fake_context.db.get_volume("tenant_a", "clone1")
        assert record["status"]["phase"] == "Failed"
        assert record["status"]["lv_created"] is False

        def grow(mount_path):
            return type(fake_context.adapters.xfs).grow(fake_context.adapters.xfs, mount_path)

        fake_context.adapters.xfs.grow = grow
        response = client.post(
            "/v1/volumes/vol1/clone",
            json={"name": "clone1", "svm": "tenant_a", "snapshot": "snap1", "size_gib": 20},
        )

        assert response.status_code == 201
        assert response.json()["data"]["volume"]["status"] == "Ready"
        assert fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_clone1")

    @pytest.mark.integration
    def test_clone_volume_resumes_existing_lv_after_stale_lease(self, fake_context):
        from arca_storage.models.volume import Volume, VolumeSpec

        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol1"})

        clone = Volume(spec=VolumeSpec(name="clone1", svm="tenant_a", size_gib=20, thin=True))
        assign_create_lease(clone.status, "dead-owner")
        fake_context.db.insert_volume(clone)
        expired_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        record = fake_context.db.get_volume("tenant_a", "clone1")
        status = record["status"]
        status["create_lease_expires_at"] = expired_at
        conn = fake_context.db._conn()
        conn.execute(
            "UPDATE volumes SET status = ? WHERE svm = ? AND name = ?",
            (json.dumps(status), "tenant_a", "clone1"),
        )
        conn.commit()
        fake_context.adapters.lvm.create_snapshot(
            "vg_pool_01",
            "vol_tenant_a_vol1_snap_snap1",
            "vol_tenant_a_clone1",
        )

        response = client.post(
            "/v1/volumes/vol1/clone",
            json={"name": "clone1", "svm": "tenant_a", "snapshot": "snap1", "size_gib": 20},
        )

        assert response.status_code == 201
        volume = response.json()["data"]["volume"]
        assert volume["status"] == "Ready"
        assert volume["size_gib"] == 20
        assert fake_context.adapters.lvm.volumes["vg_pool_01/vol_tenant_a_clone1"] == 20
        assert fake_context.adapters.xfs.mount_options["/exports/tenant_a/clone1"] == ["nouuid"]

    @pytest.mark.integration
    def test_clone_volume_rejects_unready_snapshot_without_target_record(self, fake_context):
        from arca_storage.models.snapshot import Snapshot, SnapshotSpec

        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        fake_context.db.insert_snapshot(Snapshot(spec=SnapshotSpec(name="snap1", svm="tenant_a", volume="vol1")))

        response = client.post(
            "/v1/volumes/vol1/clone",
            json={"name": "clone1", "svm": "tenant_a", "snapshot": "snap1"},
        )

        assert response.status_code == 412
        assert response.json()["error"]["code"] == "PRECONDITION_FAILED"
        assert response.json()["error"]["details"]["phase"] == "Pending"
        assert fake_context.db.get_volume("tenant_a", "clone1") is None
        assert not fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_clone1")

    @pytest.mark.integration
    def test_clone_volume_rejects_snapshot_with_missing_source_without_target_record(self, fake_context):
        from arca_storage.models.base import Phase
        from arca_storage.models.snapshot import Snapshot, SnapshotSpec

        client = TestClient(app)
        create_test_svm(client)
        snapshot = Snapshot(spec=SnapshotSpec(name="snap1", svm="tenant_a", volume="missing"))
        snapshot.status.phase = Phase.READY
        snapshot.status.lv_created = True
        fake_context.db.insert_snapshot(snapshot)

        response = client.post(
            "/v1/volumes/missing/clone",
            json={"name": "clone1", "svm": "tenant_a", "snapshot": "snap1"},
        )

        assert response.status_code == 412
        assert response.json()["error"]["code"] == "PRECONDITION_FAILED"
        assert response.json()["error"]["details"]["source_volume"] == "tenant_a/missing"
        assert fake_context.db.get_volume("tenant_a", "clone1") is None
        assert not fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_clone1")


class TestSnapshots:
    """Tests for API snapshot behavior."""

    @pytest.mark.integration
    def test_list_snapshots_returns_public_shape(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol1"})

        response = client.get("/v1/snapshots?svm=tenant_a&volume=vol1")

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["name"] == "snap1"
        assert item["svm"] == "tenant_a"
        assert item["volume"] == "vol1"
        assert item["lv_name"] == "vol_tenant_a_vol1_snap_snap1"
        assert "spec" not in item
        assert "status" in item

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
    def test_create_snapshot_rejects_unready_source_volume(self, fake_context):
        from arca_storage.models.volume import Volume, VolumeSpec

        client = TestClient(app)
        create_test_svm(client)
        fake_context.db.insert_volume(Volume(spec=VolumeSpec(name="vol1", svm="tenant_a", size_gib=10)))

        response = client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol1"})

        assert response.status_code == 412
        assert response.json()["error"]["code"] == "PRECONDITION_FAILED"
        assert response.json()["error"]["details"]["phase"] == "Pending"
        assert fake_context.db.list_snapshots(svm="tenant_a", volume="vol1") == []
        assert not fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_vol1_snap_snap1")

    @pytest.mark.integration
    def test_delete_snapshot_reports_reconciler_failure(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        client.post("/v1/snapshots", json={"name": "snap1", "svm": "tenant_a", "volume": "vol1"})

        def fail_delete(_vg, _lv):
            record = fake_context.db.list_snapshots(svm="tenant_a", volume="vol1", name="snap1")[0]
            assert record["status"]["phase"] == "Deleting"
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
    def test_delete_volume_uses_persisted_mount_path_after_export_dir_change(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})
        switch_export_dir(fake_context, "/newexports")

        response = client.delete("/v1/volumes/vol1?svm=tenant_a")

        assert response.status_code == 200
        assert "/exports/tenant_a/vol1" not in fake_context.adapters.xfs.mounts
        assert fake_context.db.get_volume("tenant_a", "vol1") is None
        assert not fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_vol1")

    @pytest.mark.integration
    def test_create_volume_does_not_resume_failed_delete(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 10})

        def fail_delete(_vg, _lv):
            record = fake_context.db.get_volume("tenant_a", "vol1")
            assert record["status"]["phase"] == "Deleting"
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


class TestListVolumes:
    @pytest.mark.integration
    def test_list_volumes_paginates_with_cursor(self, fake_context):
        client = TestClient(app)
        create_test_svm(client)
        for name in ("vol1", "vol2", "vol3"):
            client.post("/v1/volumes", json={"name": name, "svm": "tenant_a", "size_gib": 10})

        first = client.get("/v1/volumes?svm=tenant_a&limit=2")

        assert first.status_code == 200
        first_data = first.json()["data"]
        assert [item["name"] for item in first_data["items"]] == ["vol1", "vol2"]
        assert first_data["next_cursor"]

        second = client.get(f"/v1/volumes?svm=tenant_a&limit=2&cursor={first_data['next_cursor']}")

        assert second.status_code == 200
        second_data = second.json()["data"]
        assert [item["name"] for item in second_data["items"]] == ["vol3"]
        assert second_data["next_cursor"] is None

    @pytest.mark.integration
    def test_list_volumes_rejects_invalid_cursor(self, fake_context):
        client = TestClient(app)

        response = client.get("/v1/volumes?cursor=not-a-cursor")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
