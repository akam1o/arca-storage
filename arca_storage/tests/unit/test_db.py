"""Tests for SQLite state database."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from arca_storage.cli.lib.validators import legacy_volume_lv_name
from arca_storage.create_resume import assign_create_lease
from arca_storage.db import StateDB, encode_cursor
from arca_storage.errors import AlreadyExistsError, ConflictError, PreconditionFailedError
from arca_storage.models.base import Phase
from arca_storage.models.export import Export, ExportSpec
from arca_storage.models.snapshot import Snapshot, SnapshotSpec
from arca_storage.models.svm import SVM, SVMSpec
from arca_storage.models.volume import Volume, VolumeSpec, VolumeStatus


@pytest.fixture
def db(tmp_path):
    state = StateDB(str(tmp_path / "test.db"))
    yield state
    state.close()


class TestStateDB:
    def test_state_db_migrates_version_one_cleanup_reservations(self, tmp_path):
        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            conn.execute("INSERT INTO schema_version (version) VALUES (1)")
            conn.commit()
        finally:
            conn.close()

        state = StateDB(str(db_path))
        try:
            version = state._conn().execute("SELECT version FROM schema_version").fetchone()[0]
            columns = {
                row["name"]
                for row in state._conn().execute("PRAGMA table_info(snapshot_cleanup_reservations)").fetchall()
            }
        finally:
            state.close()

        assert version == 3
        assert {"svm", "volume", "name", "owner", "expires_at", "created_at"} <= columns

    def test_state_db_migrates_version_two_backend_lv_names(self, tmp_path):
        db_path = tmp_path / "state.db"
        state = StateDB(str(db_path))
        try:
            volume = Volume(spec=VolumeSpec(name="data", svm="svm1", size_gib=1))
            state.insert_volume(volume)
        finally:
            state.close()

        conn = sqlite3.connect(db_path)
        try:
            status = json.loads(conn.execute("SELECT status FROM volumes").fetchone()[0])
            status.pop("lv_name", None)
            conn.execute("UPDATE volumes SET status = ?", (json.dumps(status),))
            conn.execute("UPDATE schema_version SET version = 2")
            conn.execute("DELETE FROM backend_lvs")
            conn.commit()
        finally:
            conn.close()

        state = StateDB(str(db_path))
        try:
            record = state.get_volume("svm1", "data")
            backend_lv = state._conn().execute("SELECT * FROM backend_lvs").fetchone()
            version = state._conn().execute("SELECT version FROM schema_version").fetchone()[0]
        finally:
            state.close()

        assert version == 3
        assert record["status"]["lv_name"] == legacy_volume_lv_name("svm1", "data")
        assert backend_lv["lv_name"] == legacy_volume_lv_name("svm1", "data")
        assert backend_lv["resource_kind"] == "volume"
        assert backend_lv["resource_key"] == "svm1/data"

    def test_state_db_rejects_current_schema_missing_columns(self, tmp_path):
        db_path = tmp_path / "bad.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            conn.execute("INSERT INTO schema_version (version) VALUES (2)")
            conn.execute(
                """CREATE TABLE volumes (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    svm         TEXT NOT NULL,
                    spec        TEXT NOT NULL,
                    status      TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    UNIQUE(svm, name)
                )"""
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(RuntimeError, match="volumes.*generation"):
            StateDB(str(db_path))

    def test_upsert_and_get_svm(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"))
        svm.status.phase = Phase.READY

        db.upsert_svm(svm)
        result = db.get_svm(svm.spec.name)

        assert result is not None
        assert result["spec"]["name"] == "svm1"
        assert result["status"]["phase"] == "Ready"

    def test_insert_svm_rejects_existing_name_without_overwrite(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"))
        db.insert_svm(svm)

        duplicate = SVM(spec=SVMSpec(name="svm1", vlan_id=200, ip_cidr="10.0.1.5/24", gateway="10.0.1.1"))
        with pytest.raises(AlreadyExistsError):
            db.insert_svm(duplicate)

        result = db.get_svm("svm1")
        assert result["spec"]["vlan_id"] == 100

    def test_insert_svm_rejects_duplicate_vip_on_same_vlan(self, db):
        db.insert_svm(
            SVM(spec=SVMSpec(name="svm1", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"))
        )

        with pytest.raises(ConflictError) as exc_info:
            db.insert_svm(
                SVM(spec=SVMSpec(name="svm2", vlan_id=100, ip_cidr="10.0.0.5/32", gateway="10.0.0.1"))
            )

        assert exc_info.value.details["conflicting_svm"] == "svm1"
        assert exc_info.value.details["ip"] == "10.0.0.5"
        assert exc_info.value.details["vlan_id"] == 100

    def test_upsert_svm_rejects_duplicate_host_network_vip(self, db):
        db.upsert_svm(SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32")))

        with pytest.raises(ConflictError) as exc_info:
            db.upsert_svm(SVM(spec=SVMSpec(name="svm2", ip_cidr="10.0.0.5/24")))

        assert exc_info.value.details["conflicting_svm"] == "svm1"
        assert exc_info.value.details["vlan_id"] is None

    def test_insert_svm_allows_same_vip_on_different_vlans(self, db):
        db.insert_svm(
            SVM(spec=SVMSpec(name="svm1", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"))
        )
        db.insert_svm(
            SVM(spec=SVMSpec(name="svm2", vlan_id=101, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"))
        )

        assert {record["spec"]["name"] for record in db.list_svms()} == {"svm1", "svm2"}

    def test_list_svms(self, db):
        for i in range(3):
            svm = SVM(spec=SVMSpec(name=f"svm{i}", vlan_id=100 + i, ip_cidr=f"10.0.{i}.5/24", gateway=f"10.0.{i}.1"))
            db.upsert_svm(svm)

        all_svms = db.list_svms()
        assert len(all_svms) == 3

        filtered = db.list_svms(name="svm1")
        assert len(filtered) == 1
        assert filtered[0]["spec"]["name"] == "svm1"

        filtered_after_cursor = db.list_svms(name="svm1", cursor=encode_cursor(["svm0"]))
        assert len(filtered_after_cursor) == 1

        filtered_at_cursor = db.list_svms(name="svm1", cursor=encode_cursor(["svm1"]))
        assert filtered_at_cursor == []

        page = db.list_svms(limit=2, cursor=encode_cursor(["svm0"]))
        assert [item["spec"]["name"] for item in page] == ["svm1", "svm2"]

    def test_delete_svm(self, db):
        svm = SVM(spec=SVMSpec(name="del", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"))
        db.upsert_svm(svm)
        assert db.get_svm(svm.spec.name) is not None

        db.delete_svm(svm.spec.name)
        assert db.get_svm(svm.spec.name) is None

    def test_reserve_svm_delete_rejects_volumes_without_marking_deleting(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32"))
        svm.status.phase = Phase.READY
        db.insert_svm(svm)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)

        with pytest.raises(PreconditionFailedError) as exc_info:
            db.reserve_svm_delete("svm1")

        assert exc_info.value.details["volume_count"] == 1
        assert db.get_svm("svm1")["status"]["phase"] == "Ready"

    def test_reserve_svm_delete_blocks_guarded_volume_create(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32"))
        svm.status.phase = Phase.READY
        db.insert_svm(svm)

        reserved = db.reserve_svm_delete("svm1")

        assert reserved["status"]["phase"] == "Deleting"
        with pytest.raises(PreconditionFailedError):
            db.insert_volume(
                Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10)),
                require_ready_svm=True,
            )

    def test_reserve_svm_delete_blocks_active_snapshot_clone_lease(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32"))
        svm.status.phase = Phase.READY
        db.insert_svm(svm)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        snap = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1"))
        snap.status.phase = Phase.READY
        snap.status.lv_created = True
        db.insert_snapshot(snap)
        db.reserve_snapshot_clone("svm1", "vol1", "snap1", "clone-owner")

        with pytest.raises(ConflictError) as exc_info:
            db.reserve_svm_delete("svm1", force=True, delete_volumes=True)

        assert exc_info.value.details["snapshots"] == ["svm1/vol1/snap1"]
        assert db.get_svm("svm1")["status"]["phase"] == "Ready"

    def test_upsert_and_list_volumes(self, db):
        for name in ("vol1", "vol2", "vol3"):
            vol = Volume(spec=VolumeSpec(name=name, svm="svm1", size_gib=10))
            vol.status.phase = Phase.READY
            db.upsert_volume(vol)

        results = db.list_volumes(svm="svm1")
        assert len(results) == 3
        assert results[0]["spec"]["name"] == "vol1"

        page = db.list_volumes(svm="svm1", limit=2, cursor=encode_cursor(["svm1", "vol1"]))
        assert [item["spec"]["name"] for item in page] == ["vol2", "vol3"]

    def test_insert_volume_rejects_existing_key_without_overwrite(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        db.insert_volume(vol)

        duplicate = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=20))
        with pytest.raises(AlreadyExistsError):
            db.insert_volume(duplicate)

        result = db.get_volume("svm1", "vol1")
        assert result["spec"]["size_gib"] == 10

    def test_insert_volume_rejects_backend_lv_name_collision(self, db):
        first = Volume(spec=VolumeSpec(name="first", svm="svm1", size_gib=1))
        first.status.lv_name = "shared-lv"
        db.insert_volume(first)

        second = Volume(spec=VolumeSpec(name="second", svm="svm1", size_gib=1))
        second.status.lv_name = "shared-lv"

        with pytest.raises(AlreadyExistsError):
            db.insert_volume(second)

    def test_create_lease_requires_expiration_before_takeover(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        assign_create_lease(vol.status, "owner-1")
        db.insert_volume(vol)

        assert db.acquire_volume_create_lease("svm1", "vol1", "owner-2") is None

        record = db.get_volume("svm1", "vol1")
        status = record["status"]
        status["create_lease_expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn = db._conn()
        conn.execute(
            "UPDATE volumes SET status = ? WHERE svm = ? AND name = ?",
            (json.dumps(status), "svm1", "vol1"),
        )
        conn.commit()

        acquired = db.acquire_volume_create_lease("svm1", "vol1", "owner-2")

        assert acquired is not None
        assert acquired["status"]["phase"] == "Creating"
        assert acquired["status"]["create_owner"] == "owner-2"
        assert db.refresh_volume_create_lease("svm1", "vol1", "owner-1") is False
        assert db.refresh_volume_create_lease("svm1", "vol1", "owner-2") is True

    def test_create_lease_takeover_requires_matching_spec(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        assign_create_lease(vol.status, "owner-1")
        db.insert_volume(vol)

        record = db.get_volume("svm1", "vol1")
        status = record["status"]
        status["create_lease_expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn = db._conn()
        conn.execute(
            "UPDATE volumes SET status = ? WHERE svm = ? AND name = ?",
            (json.dumps(status), "svm1", "vol1"),
        )
        conn.commit()

        mismatched_spec = VolumeSpec(name="vol1", svm="svm1", size_gib=20).model_dump(mode="json")

        assert db.acquire_volume_create_lease(
            "svm1",
            "vol1",
            "owner-2",
            expected_spec=mismatched_spec,
        ) is None
        assert db.get_volume("svm1", "vol1")["status"]["create_owner"] == "owner-1"

        acquired = db.acquire_volume_create_lease(
            "svm1",
            "vol1",
            "owner-3",
            expected_spec=vol.spec.model_dump(mode="json"),
        )

        assert acquired is not None
        assert acquired["status"]["create_owner"] == "owner-3"

    def test_create_guarded_upsert_rejects_lost_lease_owner(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        assign_create_lease(vol.status, "owner-1")
        db.insert_volume(vol)

        record = db.get_volume("svm1", "vol1")
        status = record["status"]
        status["create_lease_expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn = db._conn()
        conn.execute(
            "UPDATE volumes SET status = ? WHERE svm = ? AND name = ?",
            (json.dumps(status), "svm1", "vol1"),
        )
        conn.commit()
        assert db.acquire_volume_create_lease(
            "svm1",
            "vol1",
            "owner-2",
            expected_spec=vol.spec.model_dump(mode="json"),
        ) is not None

        stale = Volume(spec=vol.spec)
        assign_create_lease(stale.status, "owner-1")
        stale.status.lv_created = True

        assert db.upsert_volume(stale, expected_create_owner="owner-1") is False
        record = db.get_volume("svm1", "vol1")
        assert record["status"]["create_owner"] == "owner-2"
        assert record["status"]["lv_created"] is False

    def test_reserve_volume_delete_rejects_snapshots_without_marking_deleting(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        db.insert_snapshot(Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1")))

        with pytest.raises(PreconditionFailedError) as exc_info:
            db.reserve_volume_delete("svm1", "vol1")

        assert exc_info.value.details["snapshot_count"] == 1
        assert db.get_volume("svm1", "vol1")["status"]["phase"] == "Ready"

    def test_reserve_volume_delete_blocks_guarded_dependents(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)

        reserved = db.reserve_volume_delete("svm1", "vol1")

        assert reserved["status"]["phase"] == "Deleting"
        assert reserved["status"]["create_owner"] is None
        with pytest.raises(PreconditionFailedError):
            db.insert_snapshot(
                Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1")),
                require_ready_volume=True,
            )
        with pytest.raises(PreconditionFailedError):
            db.upsert_export(
                Export(spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24")),
                require_ready_volume=True,
            )

    def test_update_ready_volume_does_not_reinsert_deleted_record(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        db.delete_volume("svm1", "vol1")

        vol.spec = VolumeSpec(name="vol1", svm="svm1", size_gib=20)
        vol.metadata.bump()

        assert db.update_ready_volume(vol) is False
        assert db.get_volume("svm1", "vol1") is None

    def test_update_ready_volume_rejects_deleting_record(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        db.reserve_volume_delete("svm1", "vol1", force=True)

        vol.spec = VolumeSpec(name="vol1", svm="svm1", size_gib=20)
        vol.metadata.bump()

        assert db.update_ready_volume(vol) is False
        record = db.get_volume("svm1", "vol1")
        assert record["status"]["phase"] == "Deleting"
        assert record["spec"]["size_gib"] == 10

    def test_volume_resize_lease_blocks_delete_paths(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32"))
        svm.status.phase = Phase.READY
        db.insert_svm(svm)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)

        reserved = db.reserve_volume_resize("svm1", "vol1", "resize-owner", 20)

        assert reserved["status"]["resize_owner"] == "resize-owner"
        with pytest.raises(ConflictError):
            db.reserve_volume_delete("svm1", "vol1")
        with pytest.raises(ConflictError):
            db.reserve_svm_delete("svm1", delete_volumes=True)

        vol.spec = VolumeSpec(name="vol1", svm="svm1", size_gib=20)
        vol.metadata.bump()
        assert db.complete_volume_resize(vol, "resize-owner") is True
        record = db.get_volume("svm1", "vol1")
        assert record["spec"]["size_gib"] == 20
        assert record["status"]["resize_owner"] is None
        assert db.reserve_volume_delete("svm1", "vol1")["status"]["phase"] == "Deleting"

    def test_volume_resize_rejects_shrink_without_lease(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32"))
        svm.status.phase = Phase.READY
        db.insert_svm(svm)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)

        with pytest.raises(PreconditionFailedError) as exc_info:
            db.reserve_volume_resize("svm1", "vol1", "resize-owner", 5)

        assert exc_info.value.details["current_size_gib"] == 10
        assert exc_info.value.details["requested_size_gib"] == 5
        record = db.get_volume("svm1", "vol1")
        assert record["spec"]["size_gib"] == 10
        assert record["status"].get("resize_owner") is None
        assert record["status"].get("resize_lease_expires_at") is None
        assert db.reserve_volume_delete("svm1", "vol1")["status"]["phase"] == "Deleting"

    def test_volume_resize_noop_does_not_reserve_lease(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32"))
        svm.status.phase = Phase.READY
        db.insert_svm(svm)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)

        reserved = db.reserve_volume_resize("svm1", "vol1", "resize-owner", 10)

        assert reserved["spec"]["size_gib"] == 10
        assert reserved["status"].get("resize_owner") is None
        assert reserved["status"].get("resize_lease_expires_at") is None
        record = db.get_volume("svm1", "vol1")
        assert record["status"].get("resize_owner") is None
        assert record["status"].get("resize_lease_expires_at") is None
        assert db.reserve_volume_delete("svm1", "vol1")["status"]["phase"] == "Deleting"

    def test_set_volume_qos_persists_and_clears_settings(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)

        assert db.set_volume_qos("svm1", "vol1", {"qos_enabled": True, "read_iops": 1000}) is True
        record = db.get_volume("svm1", "vol1")
        assert record["status"]["qos"]["read_iops"] == 1000

        assert db.set_volume_qos("svm1", "vol1", None) is True

        assert "qos" not in db.get_volume("svm1", "vol1")["status"]

    def test_complete_volume_resize_preserves_qos_settings(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32"))
        svm.status.phase = Phase.READY
        db.insert_svm(svm)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        db.set_volume_qos("svm1", "vol1", {"qos_enabled": True, "read_iops": 1000})
        db.reserve_volume_resize("svm1", "vol1", "resize-owner", 20)
        record = db.get_volume("svm1", "vol1")
        resized = Volume(
            spec=VolumeSpec(name="vol1", svm="svm1", size_gib=20),
            status=VolumeStatus.model_validate(record["status"]),
        )
        resized.status.phase = Phase.READY

        assert db.complete_volume_resize(resized, "resize-owner") is True

        assert db.get_volume("svm1", "vol1")["status"]["qos"]["read_iops"] == 1000

    def test_volume_resize_lease_blocks_guarded_dependents(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32"))
        svm.status.phase = Phase.READY
        db.insert_svm(svm)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        db.reserve_volume_resize("svm1", "vol1", "resize-owner", 20)

        with pytest.raises(ConflictError):
            db.insert_snapshot(
                Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1")),
                require_ready_volume=True,
                require_ready_svm=True,
            )
        with pytest.raises(ConflictError):
            db.upsert_export(
                Export(spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24")),
                require_ready_volume=True,
                require_ready_svm=True,
            )

    def test_guarded_snapshot_resume_rejects_deleting_volume(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        snap = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1"))
        snap.status.phase = Phase.FAILED
        db.insert_snapshot(snap)

        db.reserve_volume_delete("svm1", "vol1", force=True)

        with pytest.raises(PreconditionFailedError):
            db.acquire_snapshot_create_lease(
                "svm1",
                "vol1",
                "snap1",
                "owner-1",
                expected_spec=snap.spec.model_dump(mode="json"),
                allow_failed=True,
                require_ready_volume=True,
            )
        with pytest.raises(PreconditionFailedError):
            db.refresh_snapshot_create_lease(
                "svm1",
                "vol1",
                "snap1",
                "owner-1",
                require_ready_volume=True,
            )
        with pytest.raises(PreconditionFailedError):
            db.upsert_snapshot(snap, require_ready_volume=True)

    def test_snapshot_clone_lease_blocks_snapshot_and_volume_delete(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        snap = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1"))
        snap.status.phase = Phase.READY
        snap.status.lv_created = True
        db.insert_snapshot(snap)

        reserved = db.reserve_snapshot_clone("svm1", "vol1", "snap1", "clone-owner")

        assert reserved["status"]["clone_leases"]["clone-owner"]
        with pytest.raises(ConflictError):
            db.reserve_snapshot_delete("svm1", "vol1", "snap1")
        with pytest.raises(ConflictError):
            db.reserve_volume_delete("svm1", "vol1", force=True)
        assert db.refresh_snapshot_clone_lease("svm1", "vol1", "snap1", "clone-owner") is True

        db.release_snapshot_clone("svm1", "vol1", "snap1", "clone-owner")
        record = db.reserve_snapshot_delete("svm1", "vol1", "snap1")

        assert record["status"]["phase"] == "Deleting"
        assert "clone_leases" not in record["status"]

    def test_expired_snapshot_clone_lease_does_not_block_delete(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        snap = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1"))
        snap.status.phase = Phase.READY
        snap.status.lv_created = True
        db.insert_snapshot(snap)
        db.reserve_snapshot_clone("svm1", "vol1", "snap1", "clone-owner")
        record = db.list_snapshots(svm="svm1", volume="vol1", name="snap1")[0]
        status = record["status"]
        status["clone_leases"]["clone-owner"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn = db._conn()
        conn.execute(
            "UPDATE snapshots SET status = ? WHERE svm = ? AND volume = ? AND name = ?",
            (json.dumps(status), "svm1", "vol1", "snap1"),
        )
        conn.commit()

        record = db.reserve_snapshot_delete("svm1", "vol1", "snap1")

        assert record["status"]["phase"] == "Deleting"
        assert "clone_leases" not in record["status"]

    def test_upsert_and_list_snapshots(self, db):
        for name in ("snap1", "snap2", "snap3"):
            snap = Snapshot(spec=SnapshotSpec(name=name, svm="svm1", volume="vol1"))
            snap.status.phase = Phase.READY
            db.upsert_snapshot(snap)

        results = db.list_snapshots(svm="svm1")
        assert len(results) == 3
        assert results[0]["spec"]["name"] == "snap1"

        page = db.list_snapshots(svm="svm1", limit=2, cursor=encode_cursor(["svm1", "vol1", "snap1"]))
        assert [item["spec"]["name"] for item in page] == ["snap2", "snap3"]

    def test_insert_snapshot_rejects_existing_key_without_overwrite(self, db):
        snap = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1"))
        db.insert_snapshot(snap)

        duplicate = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1"))
        duplicate.status.phase = Phase.READY
        with pytest.raises(AlreadyExistsError):
            db.insert_snapshot(duplicate)

        result = db.list_snapshots(svm="svm1", volume="vol1", name="snap1")
        assert result[0]["status"]["phase"] == "Pending"

    def test_insert_snapshot_rejects_active_cleanup_reservation(self, db):
        assert db.reserve_snapshot_cleanup("svm1", "vol1", "snap1", "cleanup-owner") is True
        conn = db._conn()
        conn.execute(
            """UPDATE snapshot_cleanup_reservations
               SET expires_at = ?
               WHERE svm = ? AND volume = ? AND name = ?
            """,
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), "svm1", "vol1", "snap1"),
        )
        conn.commit()

        with pytest.raises(AlreadyExistsError):
            db.insert_snapshot(Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1")))

        assert db.reserve_snapshot_cleanup("svm1", "vol1", "snap1", "cleanup-owner-2") is True
        db.release_snapshot_cleanup("svm1", "vol1", "snap1", "cleanup-owner-2")
        db.insert_snapshot(Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1")))

        assert db.reserve_snapshot_cleanup("svm1", "vol1", "snap1", "cleanup-owner") is False

    def test_upsert_snapshot_rejects_active_cleanup_reservation_for_new_record(self, db):
        assert db.reserve_snapshot_cleanup("svm1", "vol1", "snap1", "cleanup-owner") is True

        with pytest.raises(AlreadyExistsError):
            db.upsert_snapshot(Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1")))

    def test_upsert_and_list_exports(self, db):
        for client in ("10.0.0.0/24", "10.0.1.0/24", "10.0.2.0/24"):
            export = Export(spec=ExportSpec(svm="svm1", volume="vol1", client=client))
            export.status.phase = Phase.READY
            db.upsert_export(export)

        results = db.list_exports(svm="svm1")
        assert len(results) == 3
        assert results[0]["spec"]["client"] == "10.0.0.0/24"

        page = db.list_exports(svm="svm1", limit=2, cursor=encode_cursor(["svm1", "vol1", "10.0.0.0/24"]))
        assert [item["spec"]["client"] for item in page] == ["10.0.1.0/24", "10.0.2.0/24"]

    def test_guarded_export_update_rejects_missing_create_owner_record(self, db):
        export = Export(spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24"))
        assign_create_lease(export.status, "owner-1")

        assert db.upsert_export(
            export,
            expected_create_owner="owner-1",
            allow_missing_create_owner=True,
        )
        db.delete_export("svm1", "vol1", "10.0.0.0/24")

        export.status.ganesha_configured = True
        assert not db.upsert_export(
            export,
            expected_create_owner="owner-1",
            allow_missing_create_owner=False,
        )
        assert db.get_export("svm1", "vol1", "10.0.0.0/24") is None

    def test_invalid_cursor_is_rejected(self, db):
        with pytest.raises(ValueError, match="Invalid pagination cursor"):
            db.list_svms(cursor="not-base64-json")

        with pytest.raises(ValueError, match="Invalid pagination cursor"):
            db.list_svms(name="svm1", cursor="not-base64-json")

    def test_operation_log(self, db):
        db.log_operation("svm", "svm1", "create", "started", "Creating SVM")
        # Should not raise — just validates the call works

    def test_transaction_rollback(self, db):
        svm = SVM(spec=SVMSpec(name="tx", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"))
        db.upsert_svm(svm)

        try:
            with db.transaction() as conn:
                conn.execute("DELETE FROM svms WHERE name = ?", (svm.spec.name,))
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

        # SVM should still exist because transaction rolled back
        assert db.get_svm(svm.spec.name) is not None
