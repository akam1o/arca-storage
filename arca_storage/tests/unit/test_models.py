"""Tests for resource models."""

from datetime import datetime, timezone

from arca_storage.models.base import Phase, ResourceMeta, resource_meta_from_record
from arca_storage.models.export import Export, ExportSpec
from arca_storage.models.snapshot import Snapshot, SnapshotSpec
from arca_storage.models.svm import SVM, SVMSpec
from arca_storage.models.volume import Volume, VolumeSpec


class TestResourceMeta:
    def test_default_id_generated(self):
        meta = ResourceMeta()
        assert meta.id is not None
        assert len(meta.id) > 0

    def test_bump_increments_generation(self):
        meta = ResourceMeta()
        assert meta.generation == 1
        meta.bump()
        assert meta.generation == 2

    def test_bump_updates_timestamp(self):
        meta = ResourceMeta()
        original = meta.updated_at
        meta.bump()
        assert meta.updated_at >= original

    def test_from_record_preserves_timestamps(self):
        meta = resource_meta_from_record(
            {
                "id": "resource-a",
                "generation": 3,
                "created_at": "2025-12-20T12:00:00Z",
                "updated_at": "2025-12-21T12:00:00Z",
            }
        )

        assert meta.id == "resource-a"
        assert meta.generation == 3
        assert meta.created_at == datetime(2025, 12, 20, 12, 0, tzinfo=timezone.utc)
        assert meta.updated_at == datetime(2025, 12, 21, 12, 0, tzinfo=timezone.utc)


class TestSVMModel:
    def test_default_status(self):
        svm = SVM(spec=SVMSpec(name="test", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1"))
        assert svm.status.phase == Phase.PENDING
        assert svm.status.namespace_created is False

    def test_spec_serialization(self):
        spec = SVMSpec(name="test", vlan_id=100, ip_cidr="10.0.0.5/24", gateway="10.0.0.1", mtu=9000)
        d = spec.model_dump()
        assert d["mtu"] == 9000
        assert d["name"] == "test"

    def test_vlan_is_optional(self):
        spec = SVMSpec(name="test", ip_cidr="10.0.0.5/32")
        assert spec.vlan_id is None


class TestVolumeModel:
    def test_defaults(self):
        vol = Volume(spec=VolumeSpec(name="v1", svm="s1", size_gib=10))
        assert vol.spec.thin is True
        assert vol.spec.fs_type == "xfs"
        assert vol.status.phase == Phase.PENDING

    def test_status_tracking(self):
        vol = Volume(spec=VolumeSpec(name="v1", svm="s1", size_gib=10))
        vol.status.lv_created = True
        vol.status.phase = Phase.READY
        assert vol.status.lv_created is True


class TestSnapshotModel:
    def test_creation(self):
        snap = Snapshot(spec=SnapshotSpec(name="s1", svm="svm1", volume="vol1"))
        assert snap.status.phase == Phase.PENDING


class TestExportModel:
    def test_defaults(self):
        export = Export(spec=ExportSpec(svm="s1", volume="v1", client="10.0.0.0/24"))
        assert export.spec.access == "RW"
        assert export.spec.root_squash is True
        assert export.spec.sec == ["sys"]
