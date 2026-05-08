"""
Tests for reconcilers using Fake adapters.

These tests validate the reconciliation logic end-to-end without
mocking subprocess calls — the Fake adapters provide in-memory
implementations.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from datetime import datetime, timedelta, timezone

import pytest

from arca_storage.adapters.ganesha import FakeGaneshaAdapter
from arca_storage.adapters.lvm import FakeLVMAdapter
from arca_storage.adapters.netns import FakeNetNSAdapter
from arca_storage.adapters.pacemaker import FakePacemakerAdapter
from arca_storage.adapters.systemd import FakeSystemdAdapter
from arca_storage.adapters.xfs import FakeXFSAdapter
from arca_storage.create_resume import assign_create_lease
from arca_storage.db import StateDB
from arca_storage.errors import AlreadyExistsError, CreateLeaseLostError, PreconditionFailedError
from arca_storage.models.base import Phase
from arca_storage.models.export import Export, ExportSpec
from arca_storage.models.snapshot import Snapshot, SnapshotSpec
from arca_storage.models.svm import SVM, SVMSpec
from arca_storage.models.volume import Volume, VolumeSpec
from arca_storage.reconcilers.adapters import Adapters
from arca_storage.reconcilers.export import ExportReconciler
from arca_storage.reconcilers.snapshot import SnapshotReconciler
from arca_storage.reconcilers.svm import SVMReconciler
from arca_storage.reconcilers.volume import VolumeReconciler


@pytest.fixture
def db(tmp_path):
    """Create SQLite DB in a temp directory."""
    state = StateDB(str(tmp_path / "state.db"))
    yield state
    state.close()


@pytest.fixture
def adapters():
    return Adapters(
        lvm=FakeLVMAdapter(),
        xfs=FakeXFSAdapter(),
        netns=FakeNetNSAdapter(),
        pacemaker=FakePacemakerAdapter(),
        ganesha=FakeGaneshaAdapter(),
        systemd=FakeSystemdAdapter(),
    )


@pytest.fixture
def config():
    return {
        "vg_name": "vg_arca",
        "thinpool_name": "thinpool",
        "export_dir": "/export",
        "parent_if": "eth0",
        "drbd_resource": "r0",
        "ganesha_config_dir": "/tmp/ganesha",
    }


# ── SVM Reconciler ────────────────────────────────────────────────


class TestSVMReconciler:
    def test_create_svm(self, db, adapters, config):
        rec = SVMReconciler(db, adapters, config=config)
        svm = SVM(
            spec=SVMSpec(
                name="test-svm",
                vlan_id=100,
                ip_cidr="10.0.0.5/24",
                gateway="10.0.0.1",
            ),
        )

        result = rec.reconcile(svm)

        assert result.status.phase == Phase.READY
        assert result.status.namespace_created is False
        assert result.status.vlan_attached is False
        assert result.status.vlan_ifname is not None
        assert result.status.ganesha_configured is True
        assert result.status.pacemaker_group_created is True

        # Verify adapter state
        assert adapters.netns.namespace_exists("test-svm") is False
        assert adapters.pacemaker.resource_exists("netns_test-svm")
        assert adapters.pacemaker.resource_exists("g_svm_test-svm")

        # Verify DB state
        records = db.list_svms(name="test-svm")
        assert len(records) == 1
        assert adapters.ganesha.bind_addrs["test-svm"] == "10.0.0.5"
        assert adapters.ganesha.host_network["test-svm"] is False

    def test_create_svm_without_vlan_uses_host_network(self, db, adapters, config):
        rec = SVMReconciler(db, adapters, config=config)
        svm = SVM(
            spec=SVMSpec(
                name="novlan",
                ip_cidr="10.0.9.5/32",
            ),
        )

        result = rec.reconcile(svm)

        assert result.status.phase == Phase.READY
        assert result.status.namespace_created is False
        assert result.status.vlan_attached is False
        assert adapters.netns.namespace_exists("novlan") is False
        assert adapters.ganesha.bind_addrs["novlan"] == "10.0.9.5"
        assert adapters.ganesha.host_network["novlan"] is True
        assert adapters.pacemaker.resources["ip_novlan"]["type"] == "IPaddr2"
        assert adapters.pacemaker.resources["ganesha_novlan"]["type"] == "nfs-ganesha-host"

    def test_delete_host_network_svm_preserves_unmanaged_namespace(self, db, adapters, config):
        rec = SVMReconciler(db, adapters, config=config)
        svm = SVM(
            spec=SVMSpec(
                name="sharedns",
                ip_cidr="10.0.9.6/32",
            ),
        )

        created = rec.reconcile(svm)
        assert created.status.phase == Phase.READY
        assert created.status.namespace_created is False

        adapters.netns.create_namespace("sharedns")
        created.status.phase = Phase.DELETING
        rec.reconcile(created)

        assert adapters.netns.namespace_exists("sharedns") is True
        assert len(db.list_svms(name="sharedns")) == 0

    def test_create_svm_idempotent(self, db, adapters, config):
        rec = SVMReconciler(db, adapters, config=config)
        svm = SVM(
            spec=SVMSpec(name="idem", vlan_id=200, ip_cidr="10.0.1.5/24", gateway="10.0.1.1"),
        )

        result1 = rec.reconcile(svm)
        assert result1.status.phase == Phase.READY

        # Reconcile again (same SVM)
        result2 = rec.reconcile(result1)
        assert result2.status.phase == Phase.READY

    def test_create_svm_root_lv_accepts_matching_existing_lv(self, db, adapters, config):
        rec = SVMReconciler(db, adapters, config=config)
        svm = SVM(
            spec=SVMSpec(
                name="root-resume",
                ip_cidr="10.0.8.5/32",
                root_volume_size_gib=10,
            ),
        )
        assign_create_lease(svm.status, "owner-1")
        db.insert_svm(svm)
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_root-resume", 10)

        result = rec.reconcile(svm)

        assert result.status.phase == Phase.READY
        assert result.status.lv_created is True
        assert result.status.fs_formatted is True
        assert db.get_svm("root-resume")["status"]["phase"] == Phase.READY.value

    def test_create_svm_root_lv_recreates_missing_recorded_lv(self, db, adapters, config):
        rec = SVMReconciler(db, adapters, config=config)
        svm = SVM(
            spec=SVMSpec(
                name="root-missing",
                ip_cidr="10.0.8.6/32",
                root_volume_size_gib=10,
            ),
        )
        assign_create_lease(svm.status, "owner-1")
        svm.status.ganesha_configured = True
        svm.status.lv_created = True
        svm.status.fs_formatted = True
        db.insert_svm(svm)

        result = rec.reconcile(svm)

        assert result.status.phase == Phase.READY
        assert result.status.lv_created is True
        assert result.status.fs_formatted is True
        assert adapters.lvm.lv_exists("vg_arca", "vol_root-missing")
        assert db.get_svm("root-missing")["status"]["phase"] == Phase.READY.value

    def test_delete_svm(self, db, adapters, config):
        rec = SVMReconciler(db, adapters, config=config)
        svm = SVM(
            spec=SVMSpec(name="del-svm", vlan_id=300, ip_cidr="10.0.2.5/24", gateway="10.0.2.1"),
        )

        created = rec.reconcile(svm)
        assert created.status.phase == Phase.READY

        # Mark for deletion
        created.status.phase = Phase.DELETING
        rec.reconcile(created)

        # Verify cleaned up
        assert not adapters.netns.namespace_exists("del-svm")
        assert len(db.list_svms(name="del-svm")) == 0

    def test_delete_legacy_vlan_svm_removes_reconciler_created_namespace(self, db, adapters, config):
        rec = SVMReconciler(db, adapters, config=config)
        svm = SVM(
            spec=SVMSpec(name="legacy-svm", vlan_id=301, ip_cidr="10.0.2.6/24", gateway="10.0.2.1"),
        )

        created = rec.reconcile(svm)
        adapters.netns.create_namespace("legacy-svm")
        created.status.namespace_created = True
        created.status.phase = Phase.DELETING

        rec.reconcile(created)

        assert adapters.netns.namespace_exists("legacy-svm") is False

    def test_delete_svm_removes_root_lv(self, db, adapters, config):
        rec = SVMReconciler(db, adapters, config=config)
        svm = SVM(
            spec=SVMSpec(
                name="rooted",
                vlan_id=300,
                ip_cidr="10.0.2.5/24",
                gateway="10.0.2.1",
                root_volume_size_gib=10,
            ),
        )

        created = rec.reconcile(svm)
        assert created.status.phase == Phase.READY
        assert adapters.lvm.lv_exists("vg_arca", "vol_rooted")

        created.status.phase = Phase.DELETING
        rec.reconcile(created)

        assert not adapters.lvm.lv_exists("vg_arca", "vol_rooted")
        assert len(db.list_svms(name="rooted")) == 0


# ── Volume Reconciler ─────────────────────────────────────────────


class TestVolumeReconciler:
    def test_create_volume(self, db, adapters, config):
        rec = VolumeReconciler(db, adapters, config=config)
        vol = Volume(
            spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10),
        )

        result = rec.reconcile(vol)

        assert result.status.phase == Phase.READY
        assert result.status.lv_created is True
        assert result.status.fs_formatted is True
        assert result.status.mounted is True
        assert result.status.lv_path is not None
        assert result.status.mount_path is not None

        # Verify adapter state
        assert adapters.lvm.lv_exists("vg_arca", "vol_svm1_vol1")

    def test_create_volume_regular_lv(self, db, adapters, config):
        rec = VolumeReconciler(db, adapters, config=config)
        vol = Volume(
            spec=VolumeSpec(name="thick", svm="svm1", size_gib=20, thin=False),
        )

        result = rec.reconcile(vol)

        assert result.status.phase == Phase.READY
        assert result.status.lv_created is True

    def test_create_volume_accepts_matching_existing_lv(self, db, adapters, config):
        rec = VolumeReconciler(db, adapters, config=config)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        assign_create_lease(vol.status, "owner-1")
        db.insert_volume(vol)
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_svm1_vol1", 10)

        result = rec.reconcile(vol)

        assert result.status.phase == Phase.READY
        assert result.status.lv_created is True
        assert result.status.fs_formatted is True
        assert result.status.mounted is True
        assert db.get_volume("svm1", "vol1")["status"]["phase"] == Phase.READY.value

    def test_create_volume_recreates_missing_recorded_lv(self, db, adapters, config):
        rec = VolumeReconciler(db, adapters, config=config)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        assign_create_lease(vol.status, "owner-1")
        vol.status.lv_created = True
        vol.status.lv_path = "/dev/vg_arca/vol_svm1_vol1"
        vol.status.lv_name = "vol_svm1_vol1"
        vol.status.fs_formatted = True
        vol.status.mounted = True
        vol.status.mount_path = "/export/svm1/vol1"
        db.insert_volume(vol)

        result = rec.reconcile(vol)

        assert result.status.phase == Phase.READY
        assert result.status.lv_created is True
        assert result.status.fs_formatted is True
        assert result.status.mounted is True
        assert adapters.lvm.lv_exists("vg_arca", "vol_svm1_vol1")
        assert adapters.xfs.is_mounted("/export/svm1/vol1")
        assert db.get_volume("svm1", "vol1")["status"]["phase"] == Phase.READY.value

    def test_create_volume_resume_uses_persisted_mount_path_after_export_dir_change(self, db, adapters, config):
        changed_config = {**config, "export_dir": "/new-export"}
        rec = VolumeReconciler(db, adapters, config=changed_config)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        assign_create_lease(vol.status, "owner-1")
        vol.status.lv_created = True
        vol.status.lv_path = "/dev/vg_arca/vol_svm1_vol1"
        vol.status.lv_name = "vol_svm1_vol1"
        vol.status.fs_formatted = True
        vol.status.mounted = True
        vol.status.mount_path = "/export/svm1/vol1"
        db.insert_volume(vol)
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_svm1_vol1", 10)
        adapters.xfs.format_xfs("/dev/vg_arca/vol_svm1_vol1")
        adapters.xfs.mount("/dev/vg_arca/vol_svm1_vol1", "/export/svm1/vol1")

        result = rec.reconcile(vol)

        assert result.status.phase == Phase.READY
        assert result.status.mount_path == "/export/svm1/vol1"
        assert adapters.xfs.is_mounted("/export/svm1/vol1")
        assert not adapters.xfs.is_mounted("/new-export/svm1/vol1")
        assert db.get_volume("svm1", "vol1")["status"]["mount_path"] == "/export/svm1/vol1"

    def test_create_volume_resume_remounts_persisted_mount_path_after_export_dir_change(self, db, adapters, config):
        changed_config = {**config, "export_dir": "/new-export"}
        rec = VolumeReconciler(db, adapters, config=changed_config)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        assign_create_lease(vol.status, "owner-1")
        vol.status.lv_created = True
        vol.status.lv_path = "/dev/vg_arca/vol_svm1_vol1"
        vol.status.lv_name = "vol_svm1_vol1"
        vol.status.fs_formatted = True
        vol.status.mounted = True
        vol.status.mount_path = "/export/svm1/vol1"
        db.insert_volume(vol)
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_svm1_vol1", 10)
        adapters.xfs.format_xfs("/dev/vg_arca/vol_svm1_vol1")

        result = rec.reconcile(vol)

        assert result.status.phase == Phase.READY
        assert result.status.mount_path == "/export/svm1/vol1"
        assert adapters.xfs.is_mounted("/export/svm1/vol1")
        assert not adapters.xfs.is_mounted("/new-export/svm1/vol1")
        assert db.get_volume("svm1", "vol1")["status"]["mount_path"] == "/export/svm1/vol1"

    def test_create_volume_rejects_existing_lv_with_wrong_type(self, db, adapters, config):
        rec = VolumeReconciler(db, adapters, config=config)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        assign_create_lease(vol.status, "owner-1")
        db.insert_volume(vol)
        adapters.lvm.create_regular_lv("vg_arca", "vol_svm1_vol1", 10)

        result = rec.reconcile(vol)

        assert result.status.phase == Phase.FAILED
        assert "not thin-provisioned" in result.status.message
        assert db.get_volume("svm1", "vol1")["status"]["phase"] == Phase.FAILED.value

    def test_create_volume_lost_lease_does_not_persist_stale_status(self, db, adapters, config):
        rec = VolumeReconciler(db, adapters, config=config)
        vol = Volume(spec=VolumeSpec(name="vol1", svm="svm1", size_gib=10))
        assign_create_lease(vol.status, "owner-1")
        db.insert_volume(vol)
        original_create = adapters.lvm.create_thin_lv

        def steal_lease_then_create(vg: str, thinpool: str, name: str, size_gib: int):
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
            return original_create(vg, thinpool, name, size_gib)

        adapters.lvm.create_thin_lv = steal_lease_then_create

        with pytest.raises(CreateLeaseLostError):
            rec.reconcile(vol)

        record = db.get_volume("svm1", "vol1")
        assert record["status"]["create_owner"] == "owner-2"
        assert record["status"]["lv_created"] is False

    def test_delete_volume(self, db, adapters, config):
        rec = VolumeReconciler(db, adapters, config=config)
        vol = Volume(
            spec=VolumeSpec(name="to-delete", svm="svm1", size_gib=5),
        )

        created = rec.reconcile(vol)
        assert created.status.phase == Phase.READY

        created.status.phase = Phase.DELETING
        rec.reconcile(created)

        assert not adapters.lvm.lv_exists("vg_arca", "vol_svm1_to-delete")
        assert len(db.list_volumes(svm="svm1", name="to-delete")) == 0

    def test_delete_volume_removes_oversized_legacy_record(self, db, adapters, config):
        rec = VolumeReconciler(db, adapters, config=config)
        vol = Volume(
            spec=VolumeSpec(name="v" * 59, svm="s" * 64, size_gib=5),
        )
        db.insert_volume(vol)

        vol.status.phase = Phase.DELETING
        result = rec.reconcile(vol)

        assert result.status.phase == Phase.DELETING
        assert db.get_volume("s" * 64, "v" * 59) is None


# ── Snapshot Reconciler ───────────────────────────────────────────


def _insert_ready_volume(db: StateDB, svm: str, name: str) -> None:
    if db.get_svm(svm) is None:
        octet = (sum(ord(char) for char in svm) % 200) + 1
        svm_record = SVM(spec=SVMSpec(name=svm, ip_cidr=f"10.250.{octet}.5/32"))
        svm_record.status.phase = Phase.READY
        db.insert_svm(svm_record)
    volume = Volume(spec=VolumeSpec(name=name, svm=svm, size_gib=1))
    volume.status.phase = Phase.READY
    db.upsert_volume(volume)


class TestSnapshotReconciler:
    def test_create_snapshot(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "data")
        # First create the source volume LV
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_svm1_data", 10)

        rec = SnapshotReconciler(db, adapters, config=config)
        snap = Snapshot(
            spec=SnapshotSpec(name="snap1", svm="svm1", volume="data"),
        )

        result = rec.reconcile(snap)

        assert result.status.phase == Phase.READY
        assert result.status.lv_created is True
        assert result.status.lv_path is not None

    def test_create_snapshot_accepts_matching_existing_lv(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "data")
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_svm1_data", 10)
        adapters.lvm.create_snapshot("vg_arca", "vol_svm1_data", "vol_svm1_data_snap_snap1")

        rec = SnapshotReconciler(db, adapters, config=config)
        snap = Snapshot(
            spec=SnapshotSpec(name="snap1", svm="svm1", volume="data"),
        )
        assign_create_lease(snap.status, "owner-1")
        db.insert_snapshot(snap)

        result = rec.reconcile(snap)

        assert result.status.phase == Phase.READY
        assert result.status.lv_created is True
        assert result.status.lv_name == "vol_svm1_data_snap_snap1"
        assert db.list_snapshots(svm="svm1", volume="data", name="snap1")[0]["status"]["phase"] == Phase.READY.value

    def test_delete_snapshot(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "data")
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_svm1_data", 10)

        rec = SnapshotReconciler(db, adapters, config=config)
        snap = Snapshot(
            spec=SnapshotSpec(name="snap-del", svm="svm1", volume="data"),
        )

        created = rec.reconcile(snap)
        assert created.status.phase == Phase.READY

        created.status.phase = Phase.DELETING
        rec.reconcile(created)

        assert len(db.list_snapshots(svm="svm1", name="snap-del")) == 0

    def test_create_snapshot_stops_when_volume_deleting_after_lv_create(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "data")
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_svm1_data", 10)
        original_create_snapshot = adapters.lvm.create_snapshot

        def delete_parent_after_snapshot(vg_name, source_lv, snap_lv):
            result = original_create_snapshot(vg_name, source_lv, snap_lv)
            db.reserve_volume_delete("svm1", "data", force=True)
            return result

        adapters.lvm.create_snapshot = delete_parent_after_snapshot

        rec = SnapshotReconciler(db, adapters, config=config)
        with pytest.raises(PreconditionFailedError):
            rec.reconcile(Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="data")))

        assert db.get_volume("svm1", "data")["status"]["phase"] == Phase.DELETING.value
        assert db.list_snapshots(svm="svm1", volume="data", name="snap1") == []
        assert not adapters.lvm.lv_exists("vg_arca", "vol_svm1_data_snap_snap1")

    def test_create_snapshot_keeps_existing_lv_when_volume_deleting_after_accept(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "data")
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_svm1_data", 10)
        adapters.lvm.create_snapshot("vg_arca", "vol_svm1_data", "vol_svm1_data_snap_snap1")
        db.reserve_volume_delete("svm1", "data", force=True)

        rec = SnapshotReconciler(db, adapters, config=config)
        with pytest.raises(PreconditionFailedError):
            rec.reconcile(Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="data")))

        assert adapters.lvm.lv_exists("vg_arca", "vol_svm1_data_snap_snap1")

    def test_create_snapshot_keeps_new_lv_when_lease_taken_over(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "data")
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_svm1_data", 10)
        snap = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="data"))
        assign_create_lease(snap.status, "owner-1", now=datetime.now(timezone.utc) - timedelta(minutes=20))
        db.insert_snapshot(snap, require_ready_volume=True)
        original_create_snapshot = adapters.lvm.create_snapshot

        def take_over_after_snapshot(vg_name, source_lv, snap_lv):
            result = original_create_snapshot(vg_name, source_lv, snap_lv)
            acquired = db.acquire_snapshot_create_lease(
                "svm1",
                "data",
                "snap1",
                "owner-2",
                expected_spec=snap.spec.model_dump(mode="json"),
                require_ready_volume=True,
            )
            assert acquired is not None
            return result

        adapters.lvm.create_snapshot = take_over_after_snapshot

        rec = SnapshotReconciler(db, adapters, config=config)
        with pytest.raises(CreateLeaseLostError):
            rec.reconcile(snap)

        assert adapters.lvm.lv_exists("vg_arca", "vol_svm1_data_snap_snap1")
        record = db.list_snapshots(svm="svm1", volume="data", name="snap1")[0]
        assert record["status"]["create_owner"] == "owner-2"

    def test_create_snapshot_cleans_new_lv_when_lost_lease_record_removed(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "data")
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_svm1_data", 10)
        snap = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="data"))
        assign_create_lease(snap.status, "owner-1")
        db.insert_snapshot(snap, require_ready_volume=True)
        original_create_snapshot = adapters.lvm.create_snapshot

        def remove_record_after_snapshot(vg_name, source_lv, snap_lv):
            result = original_create_snapshot(vg_name, source_lv, snap_lv)
            db.reserve_volume_delete("svm1", "data", force=True)
            db.delete_snapshot("svm1", "data", "snap1")
            return result

        adapters.lvm.create_snapshot = remove_record_after_snapshot

        rec = SnapshotReconciler(db, adapters, config=config)
        with pytest.raises(CreateLeaseLostError):
            rec.reconcile(snap)

        assert db.get_volume("svm1", "data")["status"]["phase"] == Phase.DELETING.value
        assert db.list_snapshots(svm="svm1", volume="data", name="snap1") == []
        assert not adapters.lvm.lv_exists("vg_arca", "vol_svm1_data_snap_snap1")

    def test_create_snapshot_cleans_new_lv_when_delete_removes_record(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "data")
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_svm1_data", 10)
        snap = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="data"))
        assign_create_lease(snap.status, "owner-1")
        db.insert_snapshot(snap, require_ready_volume=True)
        original_create_snapshot = adapters.lvm.create_snapshot

        def delete_snapshot_record_after_snapshot(vg_name, source_lv, snap_lv):
            result = original_create_snapshot(vg_name, source_lv, snap_lv)
            db.delete_snapshot("svm1", "data", "snap1")
            return result

        adapters.lvm.create_snapshot = delete_snapshot_record_after_snapshot

        rec = SnapshotReconciler(db, adapters, config=config)
        with pytest.raises(CreateLeaseLostError):
            rec.reconcile(snap)

        assert db.get_volume("svm1", "data")["status"]["phase"] == Phase.READY.value
        assert db.list_snapshots(svm="svm1", volume="data", name="snap1") == []
        assert not adapters.lvm.lv_exists("vg_arca", "vol_svm1_data_snap_snap1")

    def test_create_snapshot_cleanup_reservation_blocks_recreate_without_global_writer_lock(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "data")
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_svm1_data", 10)
        snap = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="data"))
        assign_create_lease(snap.status, "owner-1")
        db.insert_snapshot(snap, require_ready_volume=True)
        original_create_snapshot = adapters.lvm.create_snapshot
        original_delete_lv = adapters.lvm.delete_lv
        recreated = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="data"))
        assign_create_lease(recreated.status, "owner-2")

        def delete_snapshot_record_after_snapshot(vg_name, source_lv, snap_lv):
            result = original_create_snapshot(vg_name, source_lv, snap_lv)
            db.delete_snapshot("svm1", "data", "snap1")
            return result

        adapters.lvm.create_snapshot = delete_snapshot_record_after_snapshot

        def delete_lv_while_cleanup_reserved(vg_name, lv_name):
            observer = StateDB(db._db_path)
            try:
                with pytest.raises(AlreadyExistsError):
                    observer.insert_snapshot(recreated, require_ready_volume=True)
                _insert_ready_volume(observer, "svm1", "side")
            finally:
                observer.close()
            original_delete_lv(vg_name, lv_name)

        adapters.lvm.delete_lv = delete_lv_while_cleanup_reserved
        rec = SnapshotReconciler(db, adapters, config=config)
        with pytest.raises(CreateLeaseLostError):
            rec.reconcile(snap)

        assert db.get_volume("svm1", "side") is not None
        assert db.list_snapshots(svm="svm1", volume="data", name="snap1") == []
        assert not adapters.lvm.lv_exists("vg_arca", "vol_svm1_data_snap_snap1")

        adapters.lvm.create_snapshot = original_create_snapshot
        adapters.lvm.delete_lv = original_delete_lv
        db.insert_snapshot(recreated, require_ready_volume=True)
        assert db.list_snapshots(svm="svm1", volume="data", name="snap1")[0]["status"]["create_owner"] == "owner-2"
        result = rec.reconcile(recreated)

        assert result.status.phase == Phase.READY
        assert adapters.lvm.lv_exists("vg_arca", "vol_svm1_data_snap_snap1")

    def test_create_snapshot_keeps_cleanup_reservation_when_untracked_lv_delete_fails(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "data")
        adapters.lvm.create_thin_lv("vg_arca", "thinpool", "vol_svm1_data", 10)
        snap = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="data"))
        assign_create_lease(snap.status, "owner-1")
        db.insert_snapshot(snap, require_ready_volume=True)
        original_create_snapshot = adapters.lvm.create_snapshot

        def delete_snapshot_record_after_snapshot(vg_name, source_lv, snap_lv):
            result = original_create_snapshot(vg_name, source_lv, snap_lv)
            db.delete_snapshot("svm1", "data", "snap1")
            return result

        def fail_delete_lv(_vg_name, _lv_name):
            raise RuntimeError("lvremove failed")

        adapters.lvm.create_snapshot = delete_snapshot_record_after_snapshot
        adapters.lvm.delete_lv = fail_delete_lv

        rec = SnapshotReconciler(db, adapters, config=config)
        with pytest.raises(CreateLeaseLostError):
            rec.reconcile(snap)

        recreated = Snapshot(spec=SnapshotSpec(name="snap1", svm="svm1", volume="data"))
        assign_create_lease(recreated.status, "owner-2")
        with pytest.raises(AlreadyExistsError):
            db.insert_snapshot(recreated, require_ready_volume=True)
        assert adapters.lvm.lv_exists("vg_arca", "vol_svm1_data_snap_snap1")

    def test_delete_snapshot_removes_oversized_legacy_record(self, db, adapters, config):
        rec = SnapshotReconciler(db, adapters, config=config)
        snap = Snapshot(
            spec=SnapshotSpec(name="p" * 64, svm="s" * 64, volume="v" * 64),
        )
        db.insert_snapshot(snap)

        snap.status.phase = Phase.DELETING
        result = rec.reconcile(snap)

        assert result.status.phase == Phase.DELETING
        assert db.list_snapshots(svm="s" * 64, volume="v" * 64, name="p" * 64) == []


# ── Export Reconciler ─────────────────────────────────────────────


class TestExportReconciler:
    def test_create_export(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "vol1")
        rec = ExportReconciler(db, adapters, config=config)
        export = Export(
            spec=ExportSpec(
                svm="svm1",
                volume="vol1",
                client="10.0.0.0/24",
                access="rw",
            ),
        )

        result = rec.reconcile(export)

        assert result.status.phase == Phase.READY
        assert result.status.ganesha_configured is True
        assert result.status.service_reloaded is True
        assert result.status.export_id == 1
        assert adapters.ganesha.exports["svm1"][0]["path"] == "/export/svm1/vol1"

    def test_create_export_commits_reservation_before_render(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "vol1")
        rec = ExportReconciler(db, adapters, config=config)
        original_render = adapters.ganesha.render_config

        def assert_reservation_visible(svm_name, exports, *, bind_addr=None, host_network=False):
            observer = StateDB(db._db_path)
            try:
                record = observer.get_export("svm1", "vol1", "10.0.0.0/24")
            finally:
                observer.close()
            assert record is not None
            assert record["status"]["phase"] == Phase.CREATING.value
            assert record["status"]["export_id"] == 1
            return original_render(svm_name, exports, bind_addr=bind_addr, host_network=host_network)

        adapters.ganesha.render_config = assert_reservation_visible

        result = rec.reconcile(Export(spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24")))

        assert result.status.phase == Phase.READY

    def test_create_export_rejects_existing_key_without_overwrite(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "vol1")
        rec = ExportReconciler(db, adapters, config=config)
        created = rec.reconcile(
            Export(spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24", access="rw"))
        )
        assert created.status.phase == Phase.READY

        with pytest.raises(AlreadyExistsError):
            rec.reconcile(Export(spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24", access="ro")))

        record = db.get_export("svm1", "vol1", "10.0.0.0/24")
        assert record["spec"]["access"] == "rw"
        assert adapters.ganesha.exports["svm1"][0]["access"] == "RW"

    def test_update_export_keeps_ready_record_on_reload_failure(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "vol1")
        rec = ExportReconciler(db, adapters, config=config)
        created = rec.reconcile(
            Export(spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24", access="rw"))
        )
        assert created.status.phase == Phase.READY

        def fail_reload(_svm, *, host_network=False):
            raise RuntimeError("reload failed")

        adapters.ganesha.reload = fail_reload

        result = rec.reconcile(
            Export(spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24", access="ro")),
            allow_update=True,
        )

        assert result.status.phase == Phase.FAILED
        record = db.get_export("svm1", "vol1", "10.0.0.0/24")
        assert record["status"]["phase"] == Phase.READY.value
        assert record["spec"]["access"] == "rw"
        assert adapters.ganesha.exports["svm1"][0]["access"] == "RW"

    def test_create_export_resumes_only_matching_live_lease_owner(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "vol1")
        rec = ExportReconciler(db, adapters, config=config)
        spec = ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24", access="rw")
        reserved = Export(spec=spec)
        assign_create_lease(reserved.status, "owner-1")
        db.upsert_export(reserved)

        rejected = Export(spec=spec)
        assign_create_lease(rejected.status, "owner-2")
        with pytest.raises(AlreadyExistsError):
            rec.reconcile(rejected)

        resumed = Export(spec=spec)
        assign_create_lease(resumed.status, "owner-1")
        result = rec.reconcile(resumed)

        assert result.status.phase == Phase.READY
        record = db.get_export("svm1", "vol1", "10.0.0.0/24")
        assert record["status"]["create_owner"] is None

    def test_create_export_stops_when_volume_deleting_after_render(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "vol1")
        rec = ExportReconciler(db, adapters, config=config)
        original_render = adapters.ganesha.render_config

        def delete_parent_after_render(svm_name, exports, *, bind_addr=None, host_network=False):
            result = original_render(svm_name, exports, bind_addr=bind_addr, host_network=host_network)
            db.reserve_volume_delete("svm1", "vol1")
            return result

        adapters.ganesha.render_config = delete_parent_after_render

        with pytest.raises(PreconditionFailedError):
            rec.reconcile(Export(spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24")))

        assert adapters.ganesha.exports["svm1"] == []
        assert db.get_volume("svm1", "vol1")["status"]["phase"] == Phase.DELETING.value

    def test_delete_export(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "vol1")
        rec = ExportReconciler(db, adapters, config=config)
        export = Export(
            spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24"),
        )

        created = rec.reconcile(export)
        assert created.status.phase == Phase.READY

        created.status.phase = Phase.DELETING
        rec.reconcile(created)

        assert len(db.list_exports(svm="svm1")) == 0
        assert adapters.ganesha.exports["svm1"] == []

    def test_delete_export_commits_deleting_before_render(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "vol1")
        rec = ExportReconciler(db, adapters, config=config)
        created = rec.reconcile(Export(spec=ExportSpec(svm="svm1", volume="vol1", client="10.0.0.0/24")))
        assert created.status.phase == Phase.READY
        original_render = adapters.ganesha.render_config

        def assert_delete_visible(svm_name, exports, *, bind_addr=None, host_network=False):
            observer = StateDB(db._db_path)
            try:
                record = observer.get_export("svm1", "vol1", "10.0.0.0/24")
            finally:
                observer.close()
            assert record is not None
            assert record["status"]["phase"] == Phase.DELETING.value
            return original_render(svm_name, exports, bind_addr=bind_addr, host_network=host_network)

        adapters.ganesha.render_config = assert_delete_visible
        created.status.phase = Phase.DELETING

        rec.reconcile(created)

        assert len(db.list_exports(svm="svm1")) == 0

    def test_concurrent_export_creates_allocate_unique_ids(self, db, adapters, config):
        for i in range(1, 5):
            _insert_ready_volume(db, "svm1", f"vol{i}")
        rec = ExportReconciler(db, adapters, config=config)

        def create_export(i: int):
            export = Export(
                spec=ExportSpec(
                    svm="svm1",
                    volume=f"vol{i}",
                    client=f"10.0.{i}.0/24",
                ),
            )
            return rec.reconcile(export).status.export_id

        with ThreadPoolExecutor(max_workers=4) as executor:
            export_ids = list(executor.map(create_export, range(1, 5)))

        assert sorted(export_ids) == [1, 2, 3, 4]
        records = db.list_exports(svm="svm1")
        assert sorted(r["status"]["export_id"] for r in records) == [1, 2, 3, 4]
        assert len(adapters.ganesha.exports["svm1"]) == 4

    def test_export_render_preserves_svm_bind_addr(self, db, adapters, config):
        svm_rec = SVMReconciler(db, adapters, config=config)
        svm_rec.reconcile(SVM(spec=SVMSpec(name="host-svm", ip_cidr="10.0.8.5/32")))
        _insert_ready_volume(db, "host-svm", "vol1")

        rec = ExportReconciler(db, adapters, config=config)
        export = Export(
            spec=ExportSpec(svm="host-svm", volume="vol1", client="10.0.0.0/24"),
        )

        result = rec.reconcile(export)

        assert result.status.phase == Phase.READY
        assert adapters.ganesha.bind_addrs["host-svm"] == "10.0.8.5"
        assert adapters.ganesha.host_network["host-svm"] is True

    def test_sync_skips_failed_exports(self, db, adapters, config):
        _insert_ready_volume(db, "svm1", "ready")
        rec = ExportReconciler(db, adapters, config=config)
        ready = rec.reconcile(
            Export(spec=ExportSpec(svm="svm1", volume="ready", client="10.0.0.0/24"))
        )
        assert ready.status.phase == Phase.READY

        failed = Export(spec=ExportSpec(svm="svm1", volume="failed", client="10.0.1.0/24"))
        failed.status.phase = Phase.FAILED
        failed.status.export_id = 99
        failed.status.path = "/export/svm1/failed"
        failed.status.pseudo = "/export/svm1/failed"
        db.upsert_export(failed)

        rec.sync_svm_config("svm1")

        assert [entry["path"] for entry in adapters.ganesha.exports["svm1"]] == ["/export/svm1/ready"]


# ── Error Handling ────────────────────────────────────────────────


class TestReconcilerErrors:
    def test_svm_failure_sets_failed_phase(self, db, adapters, config):
        """If an adapter raises, the reconciler sets phase=FAILED."""
        # Make pacemaker fail by breaking its internal state
        adapters.pacemaker = FakePacemakerAdapter()
        adapters.pacemaker.groups = None  # Force TypeError on 'in' check

        rec = SVMReconciler(db, adapters, config=config)
        svm = SVM(
            spec=SVMSpec(name="fail-svm", vlan_id=400, ip_cidr="10.0.3.5/24", gateway="10.0.3.1"),
        )

        result = rec.reconcile(svm)
        assert result.status.phase == Phase.FAILED
        assert result.status.message is not None

    def test_failed_svm_create_can_resume_from_persisted_steps(self, db, adapters, config):
        adapters.pacemaker = FakePacemakerAdapter()
        adapters.pacemaker.groups = None

        rec = SVMReconciler(db, adapters, config=config)
        svm = SVM(
            spec=SVMSpec(name="retry-svm", vlan_id=401, ip_cidr="10.0.4.5/24", gateway="10.0.4.1"),
        )

        failed = rec.reconcile(svm)
        assert failed.status.phase == Phase.FAILED
        assert failed.status.namespace_created is False
        assert failed.status.pacemaker_group_created is False

        adapters.pacemaker.groups = {}
        resumed = rec.reconcile(failed)

        assert resumed.status.phase == Phase.READY
        assert resumed.status.pacemaker_group_created is True
        assert db.get_svm("retry-svm")["status"]["phase"] == Phase.READY.value
