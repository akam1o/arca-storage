"""
Integration tests for CSI compatibility endpoints.
"""

import pytest

from fastapi.testclient import TestClient

from arca_storage.api.main import app
from arca_storage.api.services import directory_service

GIB = 1024**3


def test_csi_directory_quota_flow_uses_svm_root_export(fake_context):
    client = TestClient(app)
    svm_name = "k8s-default"
    volume_path = "pvc-1234567890abcdef"

    response = client.post(
        "/v1/svms",
        json={
            "name": svm_name,
            "vlan_id": 100,
            "ip_cidr": "192.168.10.5/24",
            "gateway": "192.168.10.1",
        },
    )
    assert response.status_code == 201

    response = client.get(f"/v1/svms/{svm_name}")
    assert response.status_code == 200
    assert response.json()["data"]["vip"] == "192.168.10.5"

    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 2 * GIB,
        },
    )
    assert response.status_code == 201
    assert response.json()["data"]["directory"]["path"] == volume_path

    record = fake_context.db.get_volume(svm_name, volume_path)
    assert record is not None
    assert record["spec"]["size_gib"] == 2

    exports = fake_context.adapters.ganesha.exports[svm_name]
    assert _export_paths(exports) == [f"/exports/{svm_name}", f"/exports/{svm_name}/{volume_path}"]
    assert all(export["owner"] == "csi" for export in exports)
    assert {(export["client"], export["squash"]) for export in exports} == {
        ("10.0.0.0/24", "Root_Squash")
    }

    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 2 * GIB,
        },
    )
    assert response.status_code == 201
    assert _export_paths(fake_context.adapters.ganesha.exports[svm_name]) == _export_paths(exports)

    response = client.post(
        "/v1/quotas",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 3 * GIB,
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["quota_bytes"] == 3 * GIB
    assert fake_context.db.get_volume(svm_name, volume_path)["spec"]["size_gib"] == 3

    response = client.get(f"/v1/quotas/{svm_name}", params={"path": volume_path})
    assert response.status_code == 200
    assert response.json()["data"]["quota_bytes"] == 3 * GIB

    response = client.delete(f"/v1/directories/{svm_name}", params={"path": volume_path})
    assert response.status_code == 200
    assert fake_context.db.get_volume(svm_name, volume_path) is None
    assert fake_context.adapters.ganesha.exports[svm_name] == []


def test_csi_directory_create_reports_effective_gib_quota(fake_context):
    client = TestClient(app)
    svm_name = "k8s-default"
    volume_path = "pvc-1234567890abcdef"

    response = client.post(
        "/v1/svms",
        json={
            "name": svm_name,
            "vlan_id": 100,
            "ip_cidr": "192.168.10.5/24",
            "gateway": "192.168.10.1",
        },
    )
    assert response.status_code == 201

    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": GIB + 1,
        },
    )

    assert response.status_code == 201
    assert response.json()["data"]["directory"]["quota_bytes"] == 2 * GIB
    assert fake_context.db.get_volume(svm_name, volume_path)["spec"]["size_gib"] == 2


def test_csi_quota_reports_filesystem_used_bytes(fake_context, monkeypatch):
    client = TestClient(app)
    svm_name = "k8s-default"
    volume_path = "pvc-1234567890abcdef"

    response = client.post(
        "/v1/svms",
        json={
            "name": svm_name,
            "vlan_id": 100,
            "ip_cidr": "192.168.10.5/24",
            "gateway": "192.168.10.1",
        },
    )
    assert response.status_code == 201

    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 2 * GIB,
        },
    )
    assert response.status_code == 201

    class FakeStatVfs:
        f_bsize = 4096
        f_frsize = 4096
        f_blocks = 100
        f_bfree = 25

    stat_paths = []

    def fake_statvfs(path):
        stat_paths.append(path)
        return FakeStatVfs()

    monkeypatch.setattr(directory_service.os, "statvfs", fake_statvfs)

    response = client.get(f"/v1/quotas/{svm_name}", params={"path": volume_path})

    assert response.status_code == 200
    assert stat_paths == [f"/exports/{svm_name}/{volume_path}"]
    assert response.json()["data"]["used_bytes"] == 75 * 4096


def test_csi_directory_delete_rejects_existing_snapshots(fake_context):
    client = TestClient(app)
    svm_name = "k8s-default"
    volume_path = "pvc-1234567890abcdef"

    response = client.post(
        "/v1/svms",
        json={
            "name": svm_name,
            "vlan_id": 100,
            "ip_cidr": "192.168.10.5/24",
            "gateway": "192.168.10.1",
        },
    )
    assert response.status_code == 201

    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 2 * GIB,
        },
    )
    assert response.status_code == 201
    exports = list(fake_context.adapters.ganesha.exports[svm_name])

    response = client.post("/v1/snapshots", json={"name": "snap1", "svm": svm_name, "volume": volume_path})
    assert response.status_code == 201

    response = client.delete(f"/v1/directories/{svm_name}", params={"path": volume_path})

    assert response.status_code == 412
    assert response.json()["error"]["code"] == "PRECONDITION_FAILED"
    assert fake_context.db.get_volume(svm_name, volume_path) is not None
    assert fake_context.db.list_snapshots(svm=svm_name, volume=volume_path, name="snap1")
    assert fake_context.adapters.ganesha.exports[svm_name] == exports


def test_csi_directory_rejects_unready_svm(fake_context):
    from arca_storage.models.svm import SVM, SVMSpec

    client = TestClient(app)
    svm_name = "k8s-default"
    volume_path = "pvc-1234567890abcdef"
    fake_context.db.insert_svm(
        SVM(spec=SVMSpec(name=svm_name, vlan_id=100, ip_cidr="192.168.10.5/24", gateway="192.168.10.1"))
    )

    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 2 * GIB,
        },
    )

    assert response.status_code == 412
    assert response.json()["error"]["code"] == "PRECONDITION_FAILED"
    assert response.json()["error"]["details"] == {
        "resource": "SVM",
        "name": svm_name,
        "phase": "Pending",
    }
    assert fake_context.db.get_volume(svm_name, volume_path) is None
    assert fake_context.adapters.ganesha.exports.get(svm_name, []) == []


def test_csi_directory_rejects_existing_unready_volume(fake_context):
    from arca_storage.models.volume import Volume, VolumeSpec

    client = TestClient(app)
    svm_name = "k8s-default"
    volume_path = "pvc-1234567890abcdef"

    response = client.post(
        "/v1/svms",
        json={
            "name": svm_name,
            "vlan_id": 100,
            "ip_cidr": "192.168.10.5/24",
            "gateway": "192.168.10.1",
        },
    )
    assert response.status_code == 201
    fake_context.db.insert_volume(Volume(spec=VolumeSpec(name=volume_path, svm=svm_name, size_gib=2)))

    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 2 * GIB,
        },
    )

    assert response.status_code == 412
    assert response.json()["error"]["code"] == "PRECONDITION_FAILED"
    assert response.json()["error"]["details"]["phase"] == "Pending"
    assert fake_context.adapters.ganesha.exports.get(svm_name, []) == []


def test_csi_directory_prunes_stale_export_clients(fake_context):
    client = TestClient(app)
    svm_name = "k8s-default"
    volume_path = "pvc-1234567890abcdef"
    other_volume_path = "pvc-fedcba0987654321"
    fake_context.settings.csi.client_cidrs = ["10.0.0.0/24", "10.1.0.0/24"]

    response = client.post(
        "/v1/svms",
        json={
            "name": svm_name,
            "vlan_id": 100,
            "ip_cidr": "192.168.10.5/24",
            "gateway": "192.168.10.1",
        },
    )
    assert response.status_code == 201

    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 2 * GIB,
        },
    )
    assert response.status_code == 201
    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": other_volume_path,
            "quota_bytes": 2 * GIB,
        },
    )
    assert response.status_code == 201
    assert _export_targets(fake_context.adapters.ganesha.exports[svm_name]) == {
        (f"/exports/{svm_name}", "10.0.0.0/24"),
        (f"/exports/{svm_name}", "10.1.0.0/24"),
        (f"/exports/{svm_name}/{volume_path}", "10.0.0.0/24"),
        (f"/exports/{svm_name}/{volume_path}", "10.1.0.0/24"),
        (f"/exports/{svm_name}/{other_volume_path}", "10.0.0.0/24"),
        (f"/exports/{svm_name}/{other_volume_path}", "10.1.0.0/24"),
    }

    fake_context.settings.csi.client_cidrs = ["10.0.0.0/24"]
    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 2 * GIB,
        },
    )

    assert response.status_code == 201
    assert _export_targets(fake_context.adapters.ganesha.exports[svm_name]) == {
        (f"/exports/{svm_name}", "10.0.0.0/24"),
        (f"/exports/{svm_name}/{volume_path}", "10.0.0.0/24"),
        (f"/exports/{svm_name}/{other_volume_path}", "10.0.0.0/24"),
    }


def test_csi_directory_normalizes_configured_client_cidrs(fake_context):
    client = TestClient(app)
    svm_name = "k8s-default"
    volume_path = "pvc-1234567890abcdef"
    fake_context.settings.csi.client_cidrs = ["10.0.0.1/24"]

    response = client.post(
        "/v1/svms",
        json={
            "name": svm_name,
            "vlan_id": 100,
            "ip_cidr": "192.168.10.5/24",
            "gateway": "192.168.10.1",
        },
    )
    assert response.status_code == 201

    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 2 * GIB,
        },
    )
    assert response.status_code == 201
    assert _export_targets(fake_context.adapters.ganesha.exports[svm_name]) == {
        (f"/exports/{svm_name}", "10.0.0.0/24"),
        (f"/exports/{svm_name}/{volume_path}", "10.0.0.0/24"),
    }

    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 2 * GIB,
        },
    )
    assert response.status_code == 201
    assert len(fake_context.adapters.ganesha.exports[svm_name]) == 2


@pytest.mark.parametrize(
    "cidr, message",
    [
        ("127.0.0.0/8", "loopback"),
        ("169.254.0.0/16", "link-local"),
        ("224.0.0.0/4", "multicast"),
        ("240.0.0.0/4", "reserved"),
    ],
)
def test_csi_directory_rejects_unsafe_configured_client_cidrs(fake_context, cidr, message):
    client = TestClient(app)
    svm_name = "k8s-default"
    volume_path = "pvc-1234567890abcdef"
    fake_context.settings.csi.client_cidrs = [cidr]

    response = client.post(
        "/v1/svms",
        json={
            "name": svm_name,
            "vlan_id": 100,
            "ip_cidr": "192.168.10.5/24",
            "gateway": "192.168.10.1",
        },
    )
    assert response.status_code == 201

    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 2 * GIB,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert message in response.json()["error"]["message"]
    assert fake_context.db.get_volume(svm_name, volume_path) is None
    assert fake_context.adapters.ganesha.exports.get(svm_name, []) == []


def test_csi_directory_requires_configured_client_cidrs(fake_context):
    client = TestClient(app)
    svm_name = "k8s-default"
    volume_path = "pvc-1234567890abcdef"
    fake_context.settings.csi.client_cidrs = []

    response = client.post(
        "/v1/svms",
        json={
            "name": svm_name,
            "vlan_id": 100,
            "ip_cidr": "192.168.10.5/24",
            "gateway": "192.168.10.1",
        },
    )
    assert response.status_code == 201

    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 2 * GIB,
        },
    )
    assert response.status_code == 412
    assert response.json()["error"]["code"] == "PRECONDITION_FAILED"
    assert response.json()["error"]["details"] == {
        "resource": "CSIExport",
        "config": "csi.client_cidrs",
    }
    assert fake_context.db.get_volume(svm_name, volume_path) is None
    assert fake_context.adapters.ganesha.exports.get(svm_name, []) == []


def test_csi_quota_requires_configured_client_cidrs_before_resize(fake_context):
    client = TestClient(app)
    svm_name = "k8s-default"
    volume_path = "pvc-1234567890abcdef"

    response = client.post(
        "/v1/svms",
        json={
            "name": svm_name,
            "vlan_id": 100,
            "ip_cidr": "192.168.10.5/24",
            "gateway": "192.168.10.1",
        },
    )
    assert response.status_code == 201

    response = client.post(
        "/v1/directories",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 2 * GIB,
        },
    )
    assert response.status_code == 201

    fake_context.settings.csi.client_cidrs = []
    response = client.post(
        "/v1/quotas",
        json={
            "svm_name": svm_name,
            "path": volume_path,
            "quota_bytes": 3 * GIB,
        },
    )

    assert response.status_code == 412
    assert fake_context.db.get_volume(svm_name, volume_path)["spec"]["size_gib"] == 2


def _export_paths(exports):
    return sorted(export["path"] for export in exports)


def _export_targets(exports):
    return {(export["path"], export["client"]) for export in exports}
