"""Tests for SQLite state database."""

import pytest

from arca_storage.db import StateDB, encode_cursor
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

    def test_list_svms(self, db):
        for i in range(3):
            svm = SVM(spec=SVMSpec(name=f"svm{i}", vlan_id=100 + i, ip_cidr=f"10.0.{i}.5/24", gateway=f"10.0.{i}.1"))
            db.upsert_svm(svm)

        all_svms = db.list_svms()
        assert len(all_svms) == 3

        filtered = db.list_svms(name="svm1")
        assert len(filtered) == 1
        assert filtered[0]["spec"]["name"] == "svm1"

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
