"""
Integration tests for CSI compatibility endpoints.
"""

from fastapi.testclient import TestClient

from arca_storage.api.main import app

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
    assert all(export["client"] == "0.0.0.0/0" for export in exports)

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


def _export_paths(exports):
    return sorted(export["path"] for export in exports)
