"""Tests for SQLite state database."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from arca_storage.create_resume import assign_create_lease
from arca_storage.db import StateDB, encode_cursor
from arca_storage.errors import AlreadyExistsError, ConflictError
from arca_storage.models.base import Phase
from arca_storage.models.export import Export, ExportSpec
from arca_storage.models.snapshot import Snapshot, SnapshotSpec
from arca_storage.models.svm import SVM, SVMSpec
from arca_storage.models.volume import Volume, VolumeSpec


@pytest.fixture
def db(tmp_path):
    state = StateDB(str(tmp_path / "test.db"))
    yield state
    state.close()


class TestStateDB:
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
