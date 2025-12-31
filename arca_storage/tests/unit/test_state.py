"""
Unit tests for state store.
"""

import os

import pytest


@pytest.mark.unit
def test_state_roundtrip(temp_dir, monkeypatch):
    monkeypatch.setenv("ARCA_STATE_DIR", str(temp_dir))

    from arca_storage.cli.lib import state

    state.upsert_svm({"name": "tenant_a", "vlan_id": 100, "ip_cidr": "192.168.0.10/24", "status": "available"})
    svms = state.list_svms()
    assert len(svms) == 1
    assert svms[0]["name"] == "tenant_a"

    state.upsert_volume({"svm": "tenant_a", "name": "vol1", "size_gib": 10, "mount_path": "/exports/tenant_a/vol1"})
    vols = state.list_volumes(svm="tenant_a")
    assert len(vols) == 1
    assert vols[0]["name"] == "vol1"

    assert state.delete_volume("tenant_a", "vol1") is True
    assert state.list_volumes(svm="tenant_a") == []

    assert state.delete_svm("tenant_a") is True
    assert state.list_svms() == []

