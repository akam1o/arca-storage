from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from arca_storage.api.services import qos_service
from arca_storage.errors import InvalidArgumentError, PreconditionFailedError


class DummyDB:
    def __init__(self, volumes, svm):
        self.volumes = volumes
        self.svm = svm
        self.persisted_qos = None

    def list_volumes(self, svm=None, name=None):
        return self.volumes

    def get_svm(self, svm):
        return self.svm

    def set_volume_qos(self, svm, name, qos):
        self.persisted_qos = qos
        if self.volumes:
            status = self.volumes[0].setdefault("status", {})
            if qos:
                status["qos"] = qos
            else:
                status.pop("qos", None)
        return True


def test_apply_qos_attaches_ganesha_process_and_writes_io_limits(monkeypatch, tmp_path):
    cgroup_base = tmp_path / "sys" / "fs" / "cgroup" / "arca"
    ctx = SimpleNamespace(
        db=DummyDB(
            volumes=[
                {
                    "spec": {},
                    "status": {"phase": "Ready", "lv_path": "/dev/vg_arca/test-vol"},
                }
            ],
            svm={"spec": {"vlan_id": 100}},
        )
    )

    monkeypatch.setattr(qos_service, "get_context", lambda: ctx)
    monkeypatch.setattr(qos_service, "_get_cgroup_base", lambda: cgroup_base)
    monkeypatch.setattr(
        qos_service,
        "_ensure_cgroup_hierarchy",
        lambda: cgroup_base.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(qos_service, "_get_ganesha_pid", lambda ctx_arg, svm: 4242)
    monkeypatch.setattr(qos_service, "_get_device_id", lambda lv_path: "8:16")

    result = qos_service.apply_qos_to_volume(
        "tenant-a",
        "test-vol",
        read_iops=1000,
        write_iops=500,
        read_bps=1048576,
        write_bps=524288,
    )

    cgroup_path = cgroup_base / "svm_tenant-a"
    assert result["cgroup_path"] == str(cgroup_path)
    assert result["qos_enabled"] is True
    assert (cgroup_path / "cgroup.procs").read_text(encoding="utf-8") == "4242"
    assert (cgroup_path / "io.max").read_text(encoding="utf-8") == (
        "8:16 rbps=1048576 wbps=524288 riops=1000 wiops=500"
    )
    assert ctx.db.persisted_qos == result


def test_apply_qos_rejects_empty_limits(monkeypatch):
    ctx = SimpleNamespace(
        db=DummyDB(
            volumes=[
                {
                    "spec": {},
                    "status": {"phase": "Ready", "lv_path": "/dev/vg_arca/test-vol"},
                }
            ],
            svm={"spec": {"vlan_id": 100}},
        )
    )

    def fail_hierarchy():
        raise AssertionError("empty QoS patch should not touch cgroups")

    monkeypatch.setattr(qos_service, "get_context", lambda: ctx)
    monkeypatch.setattr(qos_service, "_ensure_cgroup_hierarchy", fail_hierarchy)

    with pytest.raises(InvalidArgumentError) as exc:
        qos_service.apply_qos_to_volume("tenant-a", "test-vol")

    assert "At least one QoS limit" in str(exc.value)


def test_qos_updates_preserve_other_device_limits(monkeypatch, tmp_path):
    cgroup_base = tmp_path / "sys" / "fs" / "cgroup" / "arca"
    cgroup_path = cgroup_base / "svm_tenant-a"
    cgroup_path.mkdir(parents=True)
    (cgroup_path / "io.max").write_text("8:1 rbps=1024\n", encoding="utf-8")
    ctx = SimpleNamespace(
        db=DummyDB(
            volumes=[
                {
                    "spec": {},
                    "status": {"phase": "Ready", "lv_path": "/dev/vg_arca/test-vol"},
                }
            ],
            svm={"spec": {"vlan_id": 100}},
        )
    )

    monkeypatch.setattr(qos_service, "get_context", lambda: ctx)
    monkeypatch.setattr(qos_service, "_get_cgroup_base", lambda: cgroup_base)
    monkeypatch.setattr(qos_service, "_ensure_cgroup_hierarchy", lambda: None)
    monkeypatch.setattr(qos_service, "_get_ganesha_pid", lambda ctx_arg, svm: 4242)
    monkeypatch.setattr(qos_service, "_get_device_id", lambda lv_path: "8:16")

    qos_service.apply_qos_to_volume("tenant-a", "test-vol", read_iops=1000)

    assert (cgroup_path / "io.max").read_text(encoding="utf-8").splitlines() == [
        "8:1 rbps=1024",
        "8:16 riops=1000",
    ]

    qos_service.remove_qos_from_volume("tenant-a", "test-vol")

    assert (cgroup_path / "io.max").read_text(encoding="utf-8").splitlines() == ["8:1 rbps=1024"]
    assert ctx.db.persisted_qos is None
    assert qos_service.get_qos_settings("tenant-a", "test-vol")["qos_enabled"] is False


def test_get_qos_reapplies_persisted_limits_when_cgroup_is_missing(monkeypatch, tmp_path):
    cgroup_base = tmp_path / "sys" / "fs" / "cgroup" / "arca"
    ctx = SimpleNamespace(
        db=DummyDB(
            volumes=[
                {
                    "spec": {},
                    "status": {
                        "phase": "Ready",
                        "lv_path": "/dev/vg_arca/test-vol",
                        "qos": {"qos_enabled": True, "read_iops": 1000},
                    },
                }
            ],
            svm={"spec": {"vlan_id": 100}},
        )
    )

    monkeypatch.setattr(qos_service, "get_context", lambda: ctx)
    monkeypatch.setattr(qos_service, "_get_cgroup_base", lambda: cgroup_base)
    monkeypatch.setattr(
        qos_service,
        "_ensure_cgroup_hierarchy",
        lambda: cgroup_base.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(qos_service, "_get_ganesha_pid", lambda ctx_arg, svm: 4242)
    monkeypatch.setattr(qos_service, "_get_device_id", lambda lv_path: "8:16")

    result = qos_service.get_qos_settings("tenant-a", "test-vol")

    cgroup_path = cgroup_base / "svm_tenant-a"
    assert result["qos_enabled"] is True
    assert result["read_iops"] == 1000
    assert (cgroup_path / "cgroup.procs").read_text(encoding="utf-8") == "4242"
    assert (cgroup_path / "io.max").read_text(encoding="utf-8") == "8:16 riops=1000"
    assert ctx.db.persisted_qos == result


@pytest.mark.parametrize(
    ("operation", "kwargs"),
    [
        ("apply", {"read_iops": 1000}),
        ("remove", {}),
        ("get", {}),
    ],
)
def test_qos_rejects_unready_volume(monkeypatch, operation, kwargs):
    ctx = SimpleNamespace(
        db=DummyDB(
            volumes=[
                {
                    "spec": {},
                    "status": {"phase": "Creating"},
                }
            ],
            svm={"spec": {"vlan_id": 100}},
        )
    )

    def fail_hierarchy():
        raise AssertionError("unready volume should not touch cgroups")

    monkeypatch.setattr(qos_service, "get_context", lambda: ctx)
    monkeypatch.setattr(qos_service, "_ensure_cgroup_hierarchy", fail_hierarchy)

    with pytest.raises(PreconditionFailedError):
        if operation == "apply":
            qos_service.apply_qos_to_volume("tenant-a", "test-vol", **kwargs)
        elif operation == "remove":
            qos_service.remove_qos_from_volume("tenant-a", "test-vol")
        else:
            qos_service.get_qos_settings("tenant-a", "test-vol")


def test_qos_rejects_ready_volume_without_lv_path(monkeypatch):
    ctx = SimpleNamespace(
        db=DummyDB(
            volumes=[
                {
                    "spec": {},
                    "status": {"phase": "Ready"},
                }
            ],
            svm={"spec": {"vlan_id": 100}},
        )
    )

    monkeypatch.setattr(qos_service, "get_context", lambda: ctx)

    with pytest.raises(PreconditionFailedError):
        qos_service.apply_qos_to_volume("tenant-a", "test-vol", read_iops=1000)


def test_get_device_id_uses_target_block_device_rdev(monkeypatch):
    device_stat = SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 16))

    monkeypatch.setattr(qos_service.os, "stat", lambda path: device_stat)

    assert qos_service._get_device_id("/dev/vg_arca/test-vol") == "8:16"
