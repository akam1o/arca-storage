"""Tests for create-resume eligibility guards."""

from datetime import datetime, timezone

from arca_storage.api.services import export_service, snapshot_service, svm_service, volume_service
from arca_storage.create_resume import STALE_CREATE_RESERVATION_AFTER
from arca_storage.models.export import ExportSpec
from arca_storage.models.snapshot import SnapshotSpec
from arca_storage.models.svm import SVMSpec
from arca_storage.models.volume import VolumeSpec


def _record(spec, status, *, updated_at=None):
    record = {"spec": spec.model_dump(), "status": status}
    if updated_at is not None:
        record["updated_at"] = updated_at
    return record


def _stale_timestamp():
    return (datetime.now(timezone.utc) - STALE_CREATE_RESERVATION_AFTER * 2).isoformat()


def test_failed_delete_records_are_not_create_resume_candidates():
    svm_spec = SVMSpec(name="tenant", vlan_id=100, ip_cidr="10.0.0.2/24")
    assert not svm_service._can_resume_create(
        _record(
            svm_spec,
            {
                "phase": "Failed",
                "message": "Delete failed: namespace delete failed",
                "namespace_created": False,
                "vlan_attached": False,
                "ganesha_configured": True,
                "pacemaker_group_created": True,
            },
        ),
        svm_spec,
    )

    volume_spec = VolumeSpec(name="vol1", svm="tenant", size_gib=10)
    assert not volume_service._can_resume_create(
        _record(
            volume_spec,
            {
                "phase": "Failed",
                "message": "Delete failed: lv delete failed",
                "lv_created": True,
                "fs_formatted": True,
                "mounted": False,
            },
        ),
        volume_spec,
    )

    snapshot_spec = SnapshotSpec(name="snap1", svm="tenant", volume="vol1")
    assert not snapshot_service._can_resume_create(
        _record(
            snapshot_spec,
            {
                "phase": "Failed",
                "message": "Delete failed: snapshot delete failed",
                "lv_created": False,
            },
        ),
        snapshot_spec,
    )

    export_spec = ExportSpec(svm="tenant", volume="vol1", client="10.0.0.0/24")
    assert not export_service._can_resume_create(
        _record(
            export_spec,
            {
                "phase": "Failed",
                "message": "Delete failed: reload failed",
                "ganesha_configured": False,
                "service_reloaded": False,
            },
        ),
        export_spec,
    )


def test_failed_create_with_pending_steps_can_resume():
    volume_spec = VolumeSpec(name="vol1", svm="tenant", size_gib=10)

    assert volume_service._can_resume_create(
        _record(
            volume_spec,
            {
                "phase": "Failed",
                "message": "Step 'mounted' failed: mount failed",
                "lv_created": True,
                "fs_formatted": True,
                "mounted": False,
            },
        ),
        volume_spec,
    )


def test_active_creating_records_are_not_create_resume_candidates():
    volume_spec = VolumeSpec(name="vol1", svm="tenant", size_gib=10)

    assert not volume_service._can_resume_create(
        _record(
            volume_spec,
            {
                "phase": "Creating",
                "message": "",
                "lv_created": True,
                "fs_formatted": False,
                "mounted": False,
            },
        ),
        volume_spec,
    )


def test_stale_active_create_reservations_can_resume():
    stale_at = _stale_timestamp()
    svm_spec = SVMSpec(name="tenant", vlan_id=100, ip_cidr="10.0.0.2/24")
    volume_spec = VolumeSpec(name="vol1", svm="tenant", size_gib=10)
    snapshot_spec = SnapshotSpec(name="snap1", svm="tenant", volume="vol1")
    export_spec = ExportSpec(svm="tenant", volume="vol1", client="10.0.0.0/24")

    assert svm_service._can_resume_create(
        _record(svm_spec, {"phase": "Pending", "message": ""}, updated_at=stale_at),
        svm_spec,
    )
    assert volume_service._can_resume_create(
        _record(volume_spec, {"phase": "Pending", "message": ""}, updated_at=stale_at),
        volume_spec,
    )
    assert snapshot_service._can_resume_create(
        _record(snapshot_spec, {"phase": "Pending", "message": ""}, updated_at=stale_at),
        snapshot_spec,
    )
    assert export_service._can_resume_create(
        _record(export_spec, {"phase": "Creating", "message": ""}, updated_at=stale_at),
        export_spec,
    )
