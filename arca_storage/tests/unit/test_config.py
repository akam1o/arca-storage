"""
Unit tests for config loader.
"""

import pytest


@pytest.mark.unit
def test_load_config_missing_file(monkeypatch, temp_dir):
    monkeypatch.setenv("ARCA_BOOTSTRAP_CONFIG_PATH", str(temp_dir / "missing-bootstrap.conf"))
    monkeypatch.setenv("ARCA_RUNTIME_CONFIG_PATH", str(temp_dir / "missing-runtime.conf"))
    from arca_storage.cli.lib.config import load_config

    cfg = load_config()
    assert cfg.vg_name == "vg_pool_01"
    assert cfg.parent_if == "bond0"


@pytest.mark.unit
def test_load_config_reads_values(monkeypatch, temp_dir):
    bootstrap_path = temp_dir / "storage-bootstrap.conf"
    bootstrap_path.write_text(
        "\n".join(
            [
                "[storage]",
                "vg_name = vg_test",
                "thinpool_name = pool_test",
                "parent_if = bond9",
                "drbd_resource = r9",
                "pacemaker_ra_vendor = local",
                "",
            ]
        ),
        encoding="utf-8",
    )
    runtime_path = temp_dir / "storage-runtime.conf"
    runtime_path.write_text(
        "\n".join(
            [
                "[storage]",
                "api_host = 0.0.0.0",
                "api_port = 18080",
                "export_dir = /exports",
                "ganesha_protocols = 3,4",
                "ganesha_mountd_port = 20048",
                "ganesha_nlm_port = 32768",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ARCA_BOOTSTRAP_CONFIG_PATH", str(bootstrap_path))
    monkeypatch.setenv("ARCA_RUNTIME_CONFIG_PATH", str(runtime_path))
    from arca_storage.cli.lib.config import load_config

    cfg = load_config()
    assert cfg.vg_name == "vg_test"
    assert cfg.thinpool_name == "pool_test"
    assert cfg.parent_if == "bond9"
    assert cfg.drbd_resource == "r9"
    assert cfg.api_host == "0.0.0.0"
    assert cfg.api_port == 18080
    assert cfg.ganesha_protocols == "3,4"
    assert cfg.ganesha_mountd_port == 20048
    assert cfg.ganesha_nlm_port == 32768
