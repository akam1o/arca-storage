"""
Tests for reconcilers using Fake adapters.

These tests validate the reconciliation logic end-to-end without
mocking subprocess calls — the Fake adapters provide in-memory
implementations.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import tempfile
from pathlib import Path

import pytest

from arca_storage.adapters.ganesha import FakeGaneshaAdapter
from arca_storage.adapters.lvm import FakeLVMAdapter
from arca_storage.adapters.netns import FakeNetNSAdapter
from arca_storage.adapters.pacemaker import FakePacemakerAdapter
from arca_storage.adapters.systemd import FakeSystemdAdapter
from arca_storage.adapters.xfs import FakeXFSAdapter
from arca_storage.db import StateDB
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
    return StateDB(str(tmp_path / "state.db"))


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
        assert result.status.namespace_created is True
        assert result.status.vlan_attached is True
        assert result.status.ganesha_configured is True
        assert result.status.pacemaker_group_created is True

        # Verify adapter state
        assert adapters.netns.namespace_exists("test-svm")
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


# ── Snapshot Reconciler ───────────────────────────────────────────


class TestSnapshotReconciler:
    def test_create_snapshot(self, db, adapters, config):
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

    def test_delete_snapshot(self, db, adapters, config):
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


# ── Export Reconciler ─────────────────────────────────────────────


class TestExportReconciler:
    def test_create_export(self, db, adapters, config):
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

    def test_delete_export(self, db, adapters, config):
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

    def test_concurrent_export_creates_allocate_unique_ids(self, db, adapters, config):
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

        rec = ExportReconciler(db, adapters, config=config)
        export = Export(
            spec=ExportSpec(svm="host-svm", volume="vol1", client="10.0.0.0/24"),
        )

        result = rec.reconcile(export)

        assert result.status.phase == Phase.READY
        assert adapters.ganesha.bind_addrs["host-svm"] == "10.0.8.5"
        assert adapters.ganesha.host_network["host-svm"] is True


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
