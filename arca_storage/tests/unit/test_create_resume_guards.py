"""Tests for create-resume eligibility guards."""

from datetime import datetime, timedelta, timezone

from arca_storage.api.services import (
    export_service,
    snapshot_service,
    svm_service,
    volume_service,
)
from arca_storage.create_resume import assign_create_lease, extend_create_lease
from arca_storage.models.base import Phase
from arca_storage.models.export import ExportSpec
from arca_storage.models.snapshot import SnapshotSpec
from arca_storage.models.svm import SVMSpec
from arca_storage.models.volume import VolumeSpec, VolumeStatus


def _record(spec, status):
    return {"spec": spec.model_dump(), "status": status}


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
    svm_spec = SVMSpec(name="tenant", vlan_id=100, ip_cidr="10.0.0.2/24")
    volume_spec = VolumeSpec(name="vol1", svm="tenant", size_gib=10)

    assert svm_service._can_resume_create(
        _record(
            svm_spec,
            {
                "phase": "Failed",
                "message": "Step 'pacemaker_group_created' failed: pcs failed",
                "namespace_created": False,
                "vlan_attached": False,
                "ganesha_configured": True,
                "pacemaker_group_created": False,
            },
        ),
        svm_spec,
    )

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


def test_failed_vlan_svm_with_completed_create_steps_cannot_resume():
    svm_spec = SVMSpec(name="tenant", vlan_id=100, ip_cidr="10.0.0.2/24")

    assert not svm_service._can_resume_create(
        _record(
            svm_spec,
            {
                "phase": "Failed",
                "message": "Manual intervention required",
                "namespace_created": False,
                "vlan_attached": False,
                "ganesha_configured": True,
                "pacemaker_group_created": True,
            },
        ),
        svm_spec,
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


def test_acquired_active_create_reservations_can_resume():
    owner = "owner-1"
    svm_spec = SVMSpec(name="tenant", vlan_id=100, ip_cidr="10.0.0.2/24")
    volume_spec = VolumeSpec(name="vol1", svm="tenant", size_gib=10)
    snapshot_spec = SnapshotSpec(name="snap1", svm="tenant", volume="vol1")
    export_spec = ExportSpec(svm="tenant", volume="vol1", client="10.0.0.0/24")

    assert svm_service._can_resume_create(
        _record(svm_spec, {"phase": "Creating", "message": "", "create_owner": owner}),
        svm_spec,
        owner=owner,
    )
    assert volume_service._can_resume_create(
        _record(
            volume_spec, {"phase": "Creating", "message": "", "create_owner": owner}
        ),
        volume_spec,
        owner=owner,
    )
    assert snapshot_service._can_resume_create(
        _record(
            snapshot_spec, {"phase": "Creating", "message": "", "create_owner": owner}
        ),
        snapshot_spec,
        owner=owner,
    )
    assert export_service._can_resume_create(
        _record(
            export_spec, {"phase": "Creating", "message": "", "create_owner": owner}
        ),
        export_spec,
        owner=owner,
    )


def test_extend_create_lease_keeps_active_status_fresh_only_while_active():
    status = VolumeStatus()
    now = datetime.now(timezone.utc)
    assign_create_lease(status, "owner-1", now=now)

    assert extend_create_lease(status, "owner-1", now=now + timedelta(minutes=1))
    assert status.phase == Phase.CREATING
    assert status.create_owner == "owner-1"
    assert status.create_lease_expires_at == now + timedelta(minutes=16)

    status.phase = Phase.READY
    assert (
        extend_create_lease(status, "owner-1", now=now + timedelta(minutes=2)) is False
    )
    assert status.phase == Phase.READY
    assert status.create_lease_expires_at == now + timedelta(minutes=16)
