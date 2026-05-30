"""Tests for SQLite state database."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import arca_storage.db as db_module
from arca_storage.cli.lib.validators import legacy_volume_lv_name
from arca_storage.create_resume import assign_create_lease
from arca_storage.db import StateDB, encode_cursor
from arca_storage.errors import (
    AlreadyExistsError,
    ConflictError,
    PreconditionFailedError,
)
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


def _create_legacy_v3_svm_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE svms (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            spec        TEXT NOT NULL,
            status      TEXT NOT NULL,
            generation  INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )"""
    )


def _insert_legacy_svm(conn: sqlite3.Connection, svm: SVM) -> None:
    conn.execute(
        """INSERT INTO svms (id, name, spec, status, generation, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            svm.metadata.id,
            svm.spec.name,
            svm.spec.model_dump_json(),
            svm.status.model_dump_json(),
            svm.metadata.generation,
            svm.metadata.created_at.isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


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
            version = (
                state._conn()
                .execute("SELECT version FROM schema_version")
                .fetchone()[0]
            )
            columns = {
                row["name"]
                for row in state._conn()
                .execute("PRAGMA table_info(snapshot_cleanup_reservations)")
                .fetchall()
            }
        finally:
            state.close()

        assert version == 4
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
            status = json.loads(
                conn.execute("SELECT status FROM volumes").fetchone()[0]
            )
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
            version = (
                state._conn()
                .execute("SELECT version FROM schema_version")
                .fetchone()[0]
            )
        finally:
            state.close()

        assert version == 4
        assert record["status"]["lv_name"] == legacy_volume_lv_name("svm1", "data")
        assert backend_lv["lv_name"] == legacy_volume_lv_name("svm1", "data")
        assert backend_lv["resource_kind"] == "volume"
        assert backend_lv["resource_key"] == "svm1/data"

    def test_state_db_migrates_version_three_svm_network_index(self, tmp_path):
        db_path = tmp_path / "state.db"
        svm = SVM(
            spec=SVMSpec(
                name="svm1", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"
            )
        )
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            conn.execute("INSERT INTO schema_version (version) VALUES (3)")
            _create_legacy_v3_svm_table(conn)
            _insert_legacy_svm(conn, svm)
            conn.commit()
        finally:
            conn.close()

        state = StateDB(str(db_path))
        try:
            version = (
                state._conn()
                .execute("SELECT version FROM schema_version")
                .fetchone()[0]
            )
            row = (
                state._conn()
                .execute(
                    "SELECT network_vlan_id, network_vip, network_key FROM svms WHERE name = ?",
                    ("svm1",),
                )
                .fetchone()
            )
            indexes = {
                index["name"]
                for index in state._conn().execute("PRAGMA index_list(svms)").fetchall()
            }
        finally:
            state.close()

        assert version == 4
        assert row["network_vlan_id"] == 100
        assert row["network_vip"] == "10.0.0.5"
        assert row["network_key"] == "vlan:100:10.0.0.5"
        assert "idx_svms_network_key" in indexes

    def test_state_db_migration_rejects_duplicate_svm_network_keys(self, tmp_path):
        db_path = tmp_path / "state.db"
        svms = [
            SVM(
                spec=SVMSpec(
                    name="svm1",
                    vlan_id=100,
                    ip_cidr="10.0.0.5/24",
                    gateway="10.0.0.1",
                )
            ),
            SVM(
                spec=SVMSpec(
                    name="svm2",
                    vlan_id=100,
                    ip_cidr="10.0.0.5/32",
                    gateway="10.0.0.1",
                )
            ),
        ]
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            conn.execute("INSERT INTO schema_version (version) VALUES (3)")
            _create_legacy_v3_svm_table(conn)
            for svm in svms:
                _insert_legacy_svm(conn, svm)
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(RuntimeError) as exc_info:
            StateDB(str(db_path))

        message = str(exc_info.value)
        assert "unique SVM network index" in message
        assert "vlan:100:10.0.0.5 used by svm1, svm2" in message
        assert "UNIQUE constraint" not in message

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
        svm = SVM(
            spec=SVMSpec(
                name="svm1", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"
            )
        )
        svm.status.phase = Phase.READY

        db.upsert_svm(svm)
        result = db.get_svm(svm.spec.name)

        assert result is not None
        assert result["spec"]["name"] == "svm1"
        assert result["status"]["phase"] == "Ready"

    def test_upsert_svm_populates_network_index_columns(self, db):
        db.upsert_svm(
            SVM(
                spec=SVMSpec(
                    name="svm1", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"
                )
            )
        )

        row = (
            db._conn()
            .execute(
                "SELECT network_vlan_id, network_vip, network_key FROM svms WHERE name = ?",
                ("svm1",),
            )
            .fetchone()
        )
        plan = (
            db._conn()
            .execute(
                "EXPLAIN QUERY PLAN SELECT name FROM svms WHERE network_key = ? AND name <> ? LIMIT 1",
                ("vlan:100:10.0.0.5", "svm2"),
            )
            .fetchall()
        )

        assert row["network_vlan_id"] == 100
        assert row["network_vip"] == "10.0.0.5"
        assert row["network_key"] == "vlan:100:10.0.0.5"
        assert any("idx_svms_network_key" in query["detail"] for query in plan)

    def test_insert_svm_rejects_existing_name_without_overwrite(self, db):
        svm = SVM(
            spec=SVMSpec(
                name="svm1", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"
            )
        )
        db.insert_svm(svm)

        duplicate = SVM(
            spec=SVMSpec(
                name="svm1", vlan_id=200, ip_cidr="10.0.1.5/24", gateway="10.0.1.1"
            )
        )
        with pytest.raises(AlreadyExistsError):
            db.insert_svm(duplicate)

        result = db.get_svm("svm1")
        assert result["spec"]["vlan_id"] == 100

    def test_insert_svm_rejects_duplicate_vip_on_same_vlan(self, db):
        db.insert_svm(
            SVM(
                spec=SVMSpec(
                    name="svm1", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"
                )
            )
        )

        with pytest.raises(ConflictError) as exc_info:
            db.insert_svm(
                SVM(
                    spec=SVMSpec(
                        name="svm2",
                        vlan_id=100,
                        ip_cidr="10.0.0.5/32",
                        gateway="10.0.0.1",
                    )
                )
            )

        assert exc_info.value.details["conflicting_svm"] == "svm1"
        assert exc_info.value.details["ip"] == "10.0.0.5"
        assert exc_info.value.details["vlan_id"] == 100

    def test_insert_svm_rejects_invalid_network_key_before_persist(self, db):
        with pytest.raises(ValueError, match="Invalid CIDR format"):
            db.insert_svm(
                SVM(spec=SVMSpec(name="bad", vlan_id=100, ip_cidr="bad:/exports/24"))
            )

        assert db.get_svm("bad") is None

    def test_upsert_svm_rejects_invalid_network_key_before_persist(self, db):
        with pytest.raises(ValueError, match="Invalid CIDR format"):
            db.upsert_svm(
                SVM(spec=SVMSpec(name="bad", vlan_id=100, ip_cidr="bad:/exports/24"))
            )

        assert db.get_svm("bad") is None

    def test_insert_svm_ignores_corrupt_existing_network_key(self, db):
        corrupt = SVM(spec=SVMSpec(name="corrupt", vlan_id=100, ip_cidr="10.0.0.6/32"))
        db.insert_svm(corrupt)
        conn = db._conn()
        spec = corrupt.spec.model_dump(mode="json")
        spec["ip_cidr"] = "bad:/exports/24"
        conn.execute(
            "UPDATE svms SET spec = ? WHERE name = ?", (json.dumps(spec), "corrupt")
        )
        conn.commit()

        db.insert_svm(
            SVM(spec=SVMSpec(name="valid", vlan_id=100, ip_cidr="10.0.0.5/32"))
        )

        assert db.get_svm("valid") is not None

    def test_upsert_svm_rejects_duplicate_host_network_vip(self, db):
        db.upsert_svm(SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32")))

        with pytest.raises(ConflictError) as exc_info:
            db.upsert_svm(SVM(spec=SVMSpec(name="svm2", ip_cidr="10.0.0.5/24")))

        assert exc_info.value.details["conflicting_svm"] == "svm1"
        assert exc_info.value.details["vlan_id"] is None

    def test_insert_svm_allows_same_vip_on_different_vlans(self, db):
        db.insert_svm(
            SVM(
                spec=SVMSpec(
                    name="svm1", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"
                )
            )
        )
        db.insert_svm(
            SVM(
                spec=SVMSpec(
                    name="svm2", vlan_id=101, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"
                )
            )
        )

        assert {record["spec"]["name"] for record in db.list_svms()} == {"svm1", "svm2"}

    def test_list_svms(self, db):
        for i in range(3):
            svm = SVM(
                spec=SVMSpec(
                    name=f"svm{i}",
                    vlan_id=100 + i,
                    ip_cidr=f"10.0.{i}.5/24",
                    gateway=f"10.0.{i}.1",
                )
            )
            db.upsert_svm(svm)

        all_svms = db.list_svms()
        assert len(all_svms) == 3

        filtered = db.list_svms(name="svm1")
        assert len(filtered) == 1
        assert filtered[0]["spec"]["name"] == "svm1"

        filtered_after_cursor = db.list_svms(
            name="svm1", cursor=encode_cursor(["svm0"])
        )
        assert len(filtered_after_cursor) == 1

        filtered_at_cursor = db.list_svms(name="svm1", cursor=encode_cursor(["svm1"]))
        assert filtered_at_cursor == []

        page = db.list_svms(limit=2, cursor=encode_cursor(["svm0"]))
        assert [item["spec"]["name"] for item in page] == ["svm1", "svm2"]

    def test_delete_svm(self, db):
        svm = SVM(
            spec=SVMSpec(
                name="del", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"
            )
        )
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

    def test_reserve_svm_delete_blocks_active_create_lease(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32"))
        assign_create_lease(svm.status, "owner-1")
        db.insert_svm(svm)

        with pytest.raises(ConflictError) as exc_info:
            db.reserve_svm_delete("svm1", force=True)

        assert exc_info.value.details["create_owner"] == "owner-1"
        assert db.get_svm("svm1")["status"]["phase"] == "Creating"

    def test_reserve_svm_delete_blocks_active_volume_create_before_marking(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32"))
        svm.status.phase = Phase.READY
        db.insert_svm(svm)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        assign_create_lease(vol.status, "owner-1")
        db.insert_volume(vol)

        with pytest.raises(ConflictError) as exc_info:
            db.reserve_svm_delete("svm1", delete_volumes=True)

        assert exc_info.value.details["volumes"] == ["svm1/vol1"]
        assert db.get_svm("svm1")["status"]["phase"] == "Ready"
        assert db.get_volume("svm1", "vol1")["status"]["phase"] == "Creating"

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

        page = db.list_volumes(
            svm="svm1", limit=2, cursor=encode_cursor(["svm1", "vol1"])
        )
        assert [item["spec"]["name"] for item in page] == ["vol2", "vol3"]

    def test_list_all_helpers_collect_multiple_pages(self, db, monkeypatch):
        monkeypatch.setattr(db_module, "_LIST_ALL_PAGE_SIZE", 2)

        for name in ("vol1", "vol2", "vol3"):
            volume = Volume(spec=VolumeSpec(name=name, svm="svm1", size_gib=10))
            volume.status.phase = Phase.READY
            db.insert_volume(volume)
        for name in ("snap1", "snap2", "snap3"):
            db.insert_snapshot(
                Snapshot(spec=SnapshotSpec(name=name, svm="svm1", volume="vol1"))
            )
        for client in ("10.0.0.0/24", "10.0.1.0/24", "10.0.2.0/24"):
            db.upsert_export(
                Export(spec=ExportSpec(svm="svm1", volume="vol1", client=client))
            )

        assert [
            record["spec"]["name"] for record in db.list_all_volumes(svm="svm1")
        ] == [
            "vol1",
            "vol2",
            "vol3",
        ]
        assert [
            record["spec"]["name"]
            for record in db.list_all_snapshots(svm="svm1", volume="vol1")
        ] == [
            "snap1",
            "snap2",
            "snap3",
        ]
        assert [
            record["spec"]["client"]
            for record in db.list_all_exports(svm="svm1", volume="vol1")
        ] == [
            "10.0.0.0/24",
            "10.0.1.0/24",
            "10.0.2.0/24",
        ]

    def test_list_all_volumes_uses_consistent_snapshot(self, db, monkeypatch):
        monkeypatch.setattr(db_module, "_LIST_ALL_PAGE_SIZE", 1)
        for name in ("vol-a", "vol-c"):
            volume = Volume(spec=VolumeSpec(name=name, svm="svm1", size_gib=10))
            volume.status.phase = Phase.READY
            db.insert_volume(volume)

        observer = StateDB(db._db_path)
        inserted = False
        original_list_page = db._list_volumes_conn

        def list_page_with_concurrent_insert(
            conn,
            svm=None,
            name=None,
            limit=100,
            cursor=None,
        ):
            nonlocal inserted
            page = original_list_page(
                conn, svm=svm, name=name, limit=limit, cursor=cursor
            )
            if not inserted and cursor is None:
                inserted = True
                volume = Volume(spec=VolumeSpec(name="vol-b", svm="svm1", size_gib=10))
                volume.status.phase = Phase.READY
                observer.insert_volume(volume)
            return page

        monkeypatch.setattr(db, "_list_volumes_conn", list_page_with_concurrent_insert)
        try:
            names = [
                record["spec"]["name"] for record in db.list_all_volumes(svm="svm1")
            ]
        finally:
            observer.close()

        assert inserted
        assert names == ["vol-a", "vol-c"]
        assert db.get_volume("svm1", "vol-b") is not None

    def test_reserve_svm_delete_checks_later_pages_before_marking(
        self, db, monkeypatch
    ):
        monkeypatch.setattr(db_module, "_LIST_ALL_PAGE_SIZE", 2)
        svm = SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32"))
        svm.status.phase = Phase.READY
        db.insert_svm(svm)
        for name in ("vol1", "vol2"):
            volume = Volume(spec=VolumeSpec(name=name, svm="svm1", size_gib=10))
            volume.status.phase = Phase.READY
            db.insert_volume(volume)
        active_volume = Volume(spec=VolumeSpec(name="vol3", svm="svm1", size_gib=10))
        assign_create_lease(active_volume.status, "owner-1")
        db.insert_volume(active_volume)

        with pytest.raises(ConflictError) as exc_info:
            db.reserve_svm_delete("svm1", delete_volumes=True)

        assert exc_info.value.details["volumes"] == ["svm1/vol3"]
        assert db.get_svm("svm1")["status"]["phase"] == "Ready"

    def test_reserve_volume_delete_checks_later_snapshot_pages_before_marking(
        self, db, monkeypatch
    ):
        monkeypatch.setattr(db_module, "_LIST_ALL_PAGE_SIZE", 2)
        volume = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        volume.status.phase = Phase.READY
        db.insert_volume(volume)
        for name in ("snap1", "snap2"):
            snapshot = Snapshot(spec=SnapshotSpec(name=name, svm="svm1", volume="vol1"))
            snapshot.status.phase = Phase.READY
            db.insert_snapshot(snapshot)
        active_snapshot = Snapshot(
            spec=SnapshotSpec(name="snap3", svm="svm1", volume="vol1")
        )
        assign_create_lease(active_snapshot.status, "owner-1")
        db.insert_snapshot(active_snapshot)

        with pytest.raises(ConflictError) as exc_info:
            db.reserve_volume_delete("svm1", "vol1", force=True)

        assert exc_info.value.details["snapshots"] == ["svm1/vol1/snap3"]
        assert db.get_volume("svm1", "vol1")["status"]["phase"] == "Ready"

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
        status["create_lease_expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
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
        status["create_lease_expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        conn = db._conn()
        conn.execute(
            "UPDATE volumes SET status = ? WHERE svm = ? AND name = ?",
            (json.dumps(status), "svm1", "vol1"),
        )
        conn.commit()

        mismatched_spec = VolumeSpec(name="vol1", svm="svm1", size_gib=20).model_dump(
            mode="json"
        )

        assert (
            db.acquire_volume_create_lease(
                "svm1",
                "vol1",
                "owner-2",
                expected_spec=mismatched_spec,
            )
            is None
        )
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
        status["create_lease_expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        conn = db._conn()
        conn.execute(
            "UPDATE volumes SET status = ? WHERE svm = ? AND name = ?",
            (json.dumps(status), "svm1", "vol1"),
        )
        conn.commit()
        assert (
            db.acquire_volume_create_lease(
                "svm1",
                "vol1",
                "owner-2",
                expected_spec=vol.spec.model_dump(mode="json"),
            )
            is not None
        )

        stale = Volume(spec=vol.spec)
        assign_create_lease(stale.status, "owner-1")
        stale.status.lv_created = True

        assert db.upsert_volume(stale, expected_create_owner="owner-1") is False
        record = db.get_volume("svm1", "vol1")
        assert record["status"]["create_owner"] == "owner-2"
        assert record["status"]["lv_created"] is False

    def test_reserve_volume_delete_blocks_active_create_lease(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        assign_create_lease(vol.status, "owner-1")
        db.insert_volume(vol)

        with pytest.raises(ConflictError) as exc_info:
            db.reserve_volume_delete("svm1", "vol1")

        assert exc_info.value.details["create_owner"] == "owner-1"
        assert db.get_volume("svm1", "vol1")["status"]["phase"] == "Creating"

    def test_reserve_volume_delete_blocks_active_snapshot_create_before_marking(
        self, db
    ):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        snap = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1"))
        assign_create_lease(snap.status, "owner-1")
        db.insert_snapshot(snap)

        with pytest.raises(ConflictError) as exc_info:
            db.reserve_volume_delete("svm1", "vol1", force=True)

        assert exc_info.value.details["snapshots"] == ["svm1/vol1/snap1"]
        assert db.get_volume("svm1", "vol1")["status"]["phase"] == "Ready"
        assert (
            db.list_snapshots(svm="svm1", volume="vol1", name="snap1")[0]["status"][
                "phase"
            ]
            == "Creating"
        )

    def test_reserve_volume_delete_blocks_active_export_create_before_marking(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        export = Export(
            spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24")
        )
        assign_create_lease(export.status, "owner-1")
        db.upsert_export(export)

        with pytest.raises(ConflictError) as exc_info:
            db.reserve_volume_delete("svm1", "vol1")

        assert exc_info.value.details["exports"] == ["svm1/vol1/10.0.0.0/24"]
        assert db.get_volume("svm1", "vol1")["status"]["phase"] == "Ready"
        assert (
            db.get_export("svm1", "vol1", "10.0.0.0/24")["status"]["phase"]
            == "Creating"
        )

    def test_reserve_volume_delete_checks_later_export_pages_before_marking(
        self, db, monkeypatch
    ):
        monkeypatch.setattr(db_module, "_LIST_ALL_PAGE_SIZE", 2)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        for client in ("10.0.0.0/24", "10.0.1.0/24"):
            ready_export = Export(
                spec=ExportSpec(svm="svm1", volume="vol1", client=client)
            )
            ready_export.status.phase = Phase.READY
            db.upsert_export(ready_export)
        active_export = Export(
            spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.2.0/24")
        )
        assign_create_lease(active_export.status, "owner-1")
        db.upsert_export(active_export)

        with pytest.raises(ConflictError) as exc_info:
            db.reserve_volume_delete("svm1", "vol1")

        assert exc_info.value.details["exports"] == ["svm1/vol1/10.0.2.0/24"]
        assert db.get_volume("svm1", "vol1")["status"]["phase"] == "Ready"

    def test_reserve_volume_delete_blocks_active_csi_root_export_create_before_marking(
        self, db
    ):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        export = Export(
            spec=ExportSpec(
                svm="svm1", volume="__csi_root__", client="10.0.0.0/24", owner="csi"
            )
        )
        assign_create_lease(export.status, "owner-1")
        db.upsert_export(export)

        with pytest.raises(ConflictError) as exc_info:
            db.reserve_volume_delete("svm1", "vol1")

        assert exc_info.value.details["exports"] == ["svm1/__csi_root__/10.0.0.0/24"]
        assert db.get_volume("svm1", "vol1")["status"]["phase"] == "Ready"
        assert (
            db.get_export("svm1", "__csi_root__", "10.0.0.0/24")["status"]["phase"]
            == "Creating"
        )

    def test_reserve_volume_delete_rejects_snapshots_without_marking_deleting(self, db):
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        db.insert_snapshot(
            Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1"))
        )

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
                Export(
                    spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24")
                ),
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

        assert (
            db.set_volume_qos("svm1", "vol1", {"qos_enabled": True, "read_iops": 1000})
            is True
        )
        record = db.get_volume("svm1", "vol1")
        assert record["status"]["qos"]["read_iops"] == 1000

        assert db.set_volume_qos("svm1", "vol1", None) is True

        assert "qos" not in db.get_volume("svm1", "vol1")["status"]

    def test_complete_volume_resize_preserves_qos_added_after_reservation(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32"))
        svm.status.phase = Phase.READY
        db.insert_svm(svm)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        record = db.reserve_volume_resize("svm1", "vol1", "resize-owner", 20)
        resized = Volume(
            spec=VolumeSpec(name="vol1", svm="svm1", size_gib=20),
            status=VolumeStatus.model_validate(record["status"]),
        )
        resized.status.phase = Phase.READY
        db.set_volume_qos("svm1", "vol1", {"qos_enabled": True, "read_iops": 1000})

        assert db.complete_volume_resize(resized, "resize-owner") is True

        assert db.get_volume("svm1", "vol1")["status"]["qos"]["read_iops"] == 1000

    def test_complete_volume_resize_preserves_qos_removed_after_reservation(self, db):
        svm = SVM(spec=SVMSpec(name="svm1", ip_cidr="10.0.0.5/32"))
        svm.status.phase = Phase.READY
        db.insert_svm(svm)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        vol.status.phase = Phase.READY
        db.insert_volume(vol)
        db.set_volume_qos("svm1", "vol1", {"qos_enabled": True, "read_iops": 1000})
        record = db.reserve_volume_resize("svm1", "vol1", "resize-owner", 20)
        resized = Volume(
            spec=VolumeSpec(name="vol1", svm="svm1", size_gib=20),
            status=VolumeStatus.model_validate(record["status"]),
        )
        resized.status.phase = Phase.READY
        db.set_volume_qos("svm1", "vol1", None)

        assert db.complete_volume_resize(resized, "resize-owner") is True

        assert "qos" not in db.get_volume("svm1", "vol1")["status"]

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
                Export(
                    spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24")
                ),
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
        assert (
            db.refresh_snapshot_clone_lease("svm1", "vol1", "snap1", "clone-owner")
            is True
        )

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
        status["clone_leases"]["clone-owner"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        conn = db._conn()
        conn.execute(
            "UPDATE snapshots SET status = ? WHERE svm = ? AND volume = ? AND name = ?",
            (json.dumps(status), "svm1", "vol1", "snap1"),
        )
        conn.commit()

        record = db.reserve_snapshot_delete("svm1", "vol1", "snap1")

        assert record["status"]["phase"] == "Deleting"
        assert "clone_leases" not in record["status"]

    def test_reserve_snapshot_delete_blocks_active_create_lease(self, db):
        snap = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1"))
        assign_create_lease(snap.status, "owner-1")
        db.insert_snapshot(snap)

        with pytest.raises(ConflictError) as exc_info:
            db.reserve_snapshot_delete("svm1", "vol1", "snap1")

        assert exc_info.value.details["create_owner"] == "owner-1"
        assert (
            db.list_snapshots(svm="svm1", volume="vol1", name="snap1")[0]["status"][
                "phase"
            ]
            == "Creating"
        )

    def test_upsert_and_list_snapshots(self, db):
        for name in ("snap1", "snap2", "snap3"):
            snap = Snapshot(spec=SnapshotSpec(name=name, svm="svm1", volume="vol1"))
            snap.status.phase = Phase.READY
            db.upsert_snapshot(snap)

        results = db.list_snapshots(svm="svm1")
        assert len(results) == 3
        assert results[0]["spec"]["name"] == "snap1"

        page = db.list_snapshots(
            svm="svm1", limit=2, cursor=encode_cursor(["svm1", "vol1", "snap1"])
        )
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
        assert (
            db.reserve_snapshot_cleanup("svm1", "vol1", "snap1", "cleanup-owner")
            is True
        )
        conn = db._conn()
        conn.execute(
            """UPDATE snapshot_cleanup_reservations
               SET expires_at = ?
               WHERE svm = ? AND volume = ? AND name = ?
            """,
            (
                (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                "svm1",
                "vol1",
                "snap1",
            ),
        )
        conn.commit()

        with pytest.raises(AlreadyExistsError):
            db.insert_snapshot(
                Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1"))
            )

        assert (
            db.reserve_snapshot_cleanup("svm1", "vol1", "snap1", "cleanup-owner-2")
            is True
        )
        db.release_snapshot_cleanup("svm1", "vol1", "snap1", "cleanup-owner-2")
        db.insert_snapshot(
            Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1"))
        )

        assert (
            db.reserve_snapshot_cleanup("svm1", "vol1", "snap1", "cleanup-owner")
            is False
        )

    def test_upsert_snapshot_rejects_active_cleanup_reservation_for_new_record(
        self, db
    ):
        assert (
            db.reserve_snapshot_cleanup("svm1", "vol1", "snap1", "cleanup-owner")
            is True
        )

        with pytest.raises(AlreadyExistsError):
            db.upsert_snapshot(
                Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="vol1"))
            )

    def test_upsert_and_list_exports(self, db):
        for client in ("10.0.0.0/24", "10.0.1.0/24", "10.0.2.0/24"):
            export = Export(spec=ExportSpec(svm="svm1", volume="vol1", client=client))
            export.status.phase = Phase.READY
            db.upsert_export(export)

        results = db.list_exports(svm="svm1")
        assert len(results) == 3
        assert results[0]["spec"]["client"] == "10.0.0.0/24"

        page = db.list_exports(
            svm="svm1", limit=2, cursor=encode_cursor(["svm1", "vol1", "10.0.0.0/24"])
        )
        assert [item["spec"]["client"] for item in page] == [
            "10.0.1.0/24",
            "10.0.2.0/24",
        ]

    def test_list_exports_filters_owner(self, db):
        for client, owner in (
            ("10.0.0.0/24", "api"),
            ("10.0.1.0/24", "csi"),
            ("10.0.2.0/24", "csi"),
        ):
            export = Export(
                spec=ExportSpec(
                    svm="svm1",
                    volume="vol1",
                    client=client,
                    owner=owner,
                )
            )
            export.status.phase = Phase.READY
            db.upsert_export(export)

        assert [
            record["spec"]["client"] for record in db.list_exports(owner="csi")
        ] == [
            "10.0.1.0/24",
            "10.0.2.0/24",
        ]
        assert [
            record["spec"]["client"]
            for record in db.list_all_exports(svm="svm1", owner="csi")
        ] == [
            "10.0.1.0/24",
            "10.0.2.0/24",
        ]

    def test_guarded_export_update_rejects_missing_create_owner_record(self, db):
        export = Export(
            spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24")
        )
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

    def test_reserve_export_delete_blocks_active_create_lease(self, db):
        export = Export(
            spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24")
        )
        assign_create_lease(export.status, "owner-1")
        db.upsert_export(export)

        with pytest.raises(ConflictError) as exc_info:
            db.reserve_export_delete("svm1", "vol1", "10.0.0.0/24")

        assert exc_info.value.details["create_owner"] == "owner-1"
        assert (
            db.get_export("svm1", "vol1", "10.0.0.0/24")["status"]["phase"]
            == "Creating"
        )

    def test_invalid_cursor_is_rejected(self, db):
        with pytest.raises(ValueError, match="Invalid pagination cursor"):
            db.list_svms(cursor="not-base64-json")

        with pytest.raises(ValueError, match="Invalid pagination cursor"):
            db.list_svms(name="svm1", cursor="not-base64-json")

    def test_operation_log(self, db):
        db.log_operation("svm", "svm1", "create", "started", "Creating SVM")
        # Should not raise — just validates the call works

    def test_operation_log_query_filters_and_paginates(self, db):
        db.log_operation("SVM", "svm1", "create", "started", "Creating SVM")
        db.log_operation("Volume", "vol1", "create", "started", "Creating volume")
        db.log_operation("SVM", "svm2", "delete", "completed", "Deleted SVM")

        svm_logs = db.list_operation_log(resource_type="SVM")

        assert [entry["resource_id"] for entry in svm_logs] == ["svm2", "svm1"]

        first_page = db.list_operation_log(limit=1)
        assert [entry["resource_id"] for entry in first_page] == ["svm2"]

        second_page = db.list_operation_log(
            limit=2,
            cursor=encode_cursor([str(first_page[-1]["id"])]),
        )
        assert [entry["resource_id"] for entry in second_page] == ["vol1", "svm1"]

        create_logs = db.list_operation_log(operation="create")
        assert [entry["resource_id"] for entry in create_logs] == ["vol1", "svm1"]

    def test_operation_log_prune_deletes_old_entries(self, db):
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        fresh_timestamp = datetime.now(timezone.utc).isoformat()
        with db.transaction() as conn:
            conn.execute(
                """INSERT INTO operation_log
                   (resource_type, resource_id, operation, phase, detail, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("SVM", "old", "create", "started", "old entry", old_timestamp),
            )
            conn.execute(
                """INSERT INTO operation_log
                   (resource_type, resource_id, operation, phase, detail, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("SVM", "fresh", "create", "started", "fresh entry", fresh_timestamp),
            )

        deleted = db.prune_operation_log(datetime.now(timezone.utc) - timedelta(days=1))

        assert deleted == 1
        assert [entry["resource_id"] for entry in db.list_operation_log()] == ["fresh"]

    def test_operation_log_indexes_are_present(self, db):
        indexes = {
            row["name"]
            for row in db._conn().execute("PRAGMA index_list(operation_log)").fetchall()
        }

        assert "idx_operation_log_resource_created" in indexes
        assert "idx_operation_log_created_at" in indexes

    def test_transaction_rollback(self, db):
        svm = SVM(
            spec=SVMSpec(
                name="tx", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"
            )
        )
        db.upsert_svm(svm)

        try:
            with db.transaction() as conn:
                conn.execute("DELETE FROM svms WHERE name = ?", (svm.spec.name,))
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

        # SVM should still exist because transaction rolled back
        assert db.get_svm(svm.spec.name) is not None
