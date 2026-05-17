"""Tests for API-side NFS export path rendering."""

from types import SimpleNamespace

import pytest

from arca_storage.api.services.svm_service import _export_root, _vip_from_ip_cidr
from arca_storage.api.services.volume_service import build_volume_export_path


def _ctx(ip_cidr: str = "192.168.10.5/24", export_dir: str = "/exports"):
    def get_svm(name: str):
        if name != "tenant_a":
            return None
        return {"spec": {"ip_cidr": ip_cidr}}

    return SimpleNamespace(
        db=SimpleNamespace(get_svm=get_svm),
        settings=SimpleNamespace(to_reconciler_config=lambda: {"export_dir": export_dir}),
    )


def test_build_volume_export_path_returns_validated_export_path():
    assert (
        build_volume_export_path(_ctx(), "tenant_a", "/exports/tenant_a/vol1", "vol1")
        == "192.168.10.5:/exports/tenant_a/vol1"
    )


def test_build_volume_export_path_preserves_optional_volume_argument_compatibility():
    assert build_volume_export_path(_ctx(), "tenant_a", "/legacy/path") == "192.168.10.5:/legacy/path"


@pytest.mark.parametrize(
    "mount_path",
    [
        "",
        None,
        "/",
        "exports/tenant_a/vol1",
        "/exports/tenant_a/../vol1",
        "/exports/tenant_a/vol1\nbad",
        "/exports/other/vol1",
        "/exports/tenant_a/other",
        "/tenant_a/vol1",
    ],
)
def test_build_volume_export_path_rejects_unsafe_mount_paths(mount_path):
    assert build_volume_export_path(_ctx(), "tenant_a", mount_path, "vol1") is None


@pytest.mark.parametrize(
    "ip_cidr",
    [
        "",
        "bad:/exports/tenant_a/vol1/24",
        "127.0.0.1/24",
        "192.168.10.0/24",
    ],
)
def test_build_volume_export_path_rejects_invalid_persisted_vip(ip_cidr):
    assert build_volume_export_path(_ctx(ip_cidr=ip_cidr), "tenant_a", "/exports/tenant_a/vol1", "vol1") is None


@pytest.mark.parametrize("ip_cidr", ["bad:/exports/tenant_a/24", "127.0.0.1/24", "192.168.10.0/24"])
def test_svm_vip_from_ip_cidr_rejects_invalid_persisted_values(ip_cidr):
    assert _vip_from_ip_cidr(ip_cidr) == ""


def test_svm_export_root_rejects_unsafe_persisted_name():
    assert _export_root("../escape", _ctx()) == "/exports"


def test_svm_export_root_falls_back_when_export_dir_is_unsafe():
    assert _export_root("tenant_a", _ctx(export_dir="/exports/../escape")) == "/exports/tenant_a"
