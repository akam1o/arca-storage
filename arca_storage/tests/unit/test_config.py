"""
Unit tests for the unified TOML config loader.
"""

import pytest


@pytest.mark.unit
def test_load_settings_missing_default_fails(monkeypatch, temp_dir):
    monkeypatch.delenv("ARCA_CONFIG_PATH", raising=False)

    from arca_storage import config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", temp_dir / "missing.toml")

    with pytest.raises(FileNotFoundError):
        config_mod.load_settings()


@pytest.mark.unit
def test_load_settings_missing_default_uses_dev_defaults_when_allowed(monkeypatch, temp_dir):
    monkeypatch.delenv("ARCA_CONFIG_PATH", raising=False)

    from arca_storage import config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", temp_dir / "missing.toml")

    cfg = config_mod.load_settings(require_file=False)
    assert cfg.storage.vg_name == "vg_pool_01"
    assert cfg.network.parent_interface == "bond0"
    assert cfg.state.db_path == "/var/lib/arca-storage/state.db"


@pytest.mark.unit
def test_load_settings_explicit_missing_file_fails(temp_dir):
    from arca_storage.config import load_settings

    with pytest.raises(FileNotFoundError):
        load_settings(temp_dir / "missing.toml")


@pytest.mark.unit
def test_load_settings_reads_toml_values(monkeypatch, temp_dir):
    config_path = temp_dir / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[storage]",
                'vg_name = "vg_test"',
                'thinpool_name = "pool_test"',
                "",
                "[network]",
                'parent_interface = "bond9"',
                'default_nfs_versions = ["4"]',
                "",
                "[cluster]",
                'pacemaker_cluster_name = "test-cluster"',
                "enable_stonith = false",
                'drbd_resource = "r9"',
                'pacemaker_ra_vendor = "custom"',
                "",
                "[api]",
                'bind = "0.0.0.0"',
                "port = 18080",
                "",
                "[timeouts]",
                "subprocess_default = 11",
                "pacemaker_operation = 22",
                "nfs_mount = 33",
                "",
                "[state]",
                f'db_path = "{temp_dir}/state.db"',
                f'runtime_dir = "{temp_dir}/runtime"',
                "",
                "[ganesha]",
                f'config_dir = "{temp_dir}/ganesha"',
                f'export_dir = "{temp_dir}/exports"',
                "protocols = [3, 4]",
                "mountd_port = 20048",
                "nlm_port = 32768",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))

    from arca_storage.config import load_settings

    cfg = load_settings()
    assert cfg.storage.vg_name == "vg_test"
    assert cfg.storage.thinpool_name == "pool_test"
    assert cfg.network.parent_interface == "bond9"
    assert cfg.cluster.drbd_resource == "r9"
    assert cfg.cluster.pacemaker_ra_vendor == "custom"
    assert cfg.api.bind == "0.0.0.0"
    assert cfg.api.port == 18080
    assert cfg.state.runtime_dir == f"{temp_dir}/runtime"
    assert cfg.ganesha.protocols == [3, 4]
    assert cfg.ganesha.mountd_port == 20048
    assert cfg.ganesha.nlm_port == 32768


@pytest.mark.unit
def test_reconciler_config_is_derived_from_toml(monkeypatch, temp_dir):
    config_path = temp_dir / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[storage]",
                'vg_name = "vg_data"',
                'thinpool_name = "thin_data"',
                "[network]",
                'parent_interface = "ens192"',
                "[cluster]",
                'drbd_resource = "arca-r1"',
                "[ganesha]",
                'export_dir = "/srv/exports"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))

    from arca_storage.config import load_settings

    assert load_settings().to_reconciler_config() == {
        "vg_name": "vg_data",
        "thinpool_name": "thin_data",
        "parent_if": "ens192",
        "export_dir": "/srv/exports",
        "drbd_resource": "arca-r1",
    }


@pytest.mark.unit
def test_systemd_env_only_exports_values_consumed_by_units(monkeypatch, temp_dir):
    config_path = temp_dir / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[ganesha]",
                'config_dir = "/srv/ganesha"',
                'export_dir = "/srv/exports"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))

    from arca_storage.config import load_settings

    rendered = load_settings().to_systemd_env()
    assert "ARCA_GANESHA_CONFIG_DIR=/srv/ganesha" in rendered
    assert "ARCA_EXPORT_DIR" not in rendered
    assert "ARCA_API_HOST" not in rendered
    assert "ARCA_STATE_DIR" not in rendered
