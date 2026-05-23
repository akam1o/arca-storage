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
def test_load_settings_missing_default_uses_dev_defaults_when_allowed(
    monkeypatch, temp_dir
):
    monkeypatch.delenv("ARCA_CONFIG_PATH", raising=False)

    from arca_storage import config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", temp_dir / "missing.toml")

    cfg = config_mod.load_settings(require_file=False)
    assert cfg.storage.vg_name == "vg_pool_01"
    assert cfg.network.parent_interface == "bond0"
    assert cfg.state.db_path == "/var/lib/arca-storage/state.db"
    assert cfg.api.ssl_certfile is None
    assert cfg.api.ssl_keyfile is None
    assert cfg.csi.client_cidrs == []
    assert cfg.csi.root_squash is True


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
                f'ssl_certfile = "{temp_dir}/tls/api.crt"',
                f'ssl_keyfile = "{temp_dir}/tls/api.key"',
                "",
                "[timeouts]",
                "subprocess_default = 11",
                "pacemaker_operation = 22",
                "nfs_mount = 33",
                "",
                "[state]",
                f'db_path = "{temp_dir}/state.db"',
                f'runtime_dir = "{temp_dir}/runtime"',
                "operation_log_retention_days = 7",
                "",
                "[ganesha]",
                f'config_dir = "{temp_dir}/ganesha"',
                f'export_dir = "{temp_dir}/exports"',
                "protocols = [3, 4]",
                "mountd_port = 20048",
                "nlm_port = 32768",
                "",
                "[csi]",
                'client_cidrs = ["10.0.0.0/24", "10.0.0.10/24"]',
                "root_squash = false",
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
    assert cfg.api.ssl_certfile == f"{temp_dir}/tls/api.crt"
    assert cfg.api.ssl_keyfile == f"{temp_dir}/tls/api.key"
    assert cfg.state.runtime_dir == f"{temp_dir}/runtime"
    assert cfg.state.operation_log_retention_days == 7
    assert cfg.ganesha.protocols == [3, 4]
    assert cfg.ganesha.mountd_port == 20048
    assert cfg.ganesha.nlm_port == 32768
    assert cfg.csi.client_cidrs == ["10.0.0.0/24"]
    assert cfg.csi.root_squash is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "cidr, match",
    [
        ("0.0.0.0/0", "default route"),
        ("10.0.0.1/0", "default route"),
        ("127.0.0.0/8", "loopback"),
        ("169.254.0.0/16", "link-local"),
        ("224.0.0.0/4", "multicast"),
        ("240.0.0.0/4", "reserved"),
    ],
)
def test_load_settings_rejects_unsafe_csi_client_cidr(
    monkeypatch, temp_dir, cidr, match
):
    config_path = temp_dir / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[csi]",
                f"client_cidrs = [{cidr!r}]",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))

    from arca_storage.config import load_settings

    with pytest.raises(ValueError, match=match):
        load_settings()


@pytest.mark.unit
@pytest.mark.parametrize("vendor", ["../etc", "/tmp", "local/bad", "..", ".hidden", ""])
def test_load_settings_rejects_unsafe_pacemaker_ra_vendor(
    monkeypatch, temp_dir, vendor
):
    config_path = temp_dir / "config.toml"
    config_path.write_text(
        f"[cluster]\npacemaker_ra_vendor = {vendor!r}\n", encoding="utf-8"
    )
    monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))

    from arca_storage.config import load_settings

    with pytest.raises(ValueError, match="cluster.pacemaker_ra_vendor"):
        load_settings()


@pytest.mark.unit
@pytest.mark.parametrize(
    "section,key,value,match",
    [
        ("storage", "vg_name", "../vg", "storage.vg_name"),
        ("storage", "vg_name", "vg/bad", "storage.vg_name"),
        ("storage", "thinpool_name", "..", "storage.thinpool_name"),
        ("storage", "thinpool_name", "", "storage.thinpool_name"),
        ("network", "parent_interface", "bond0/1", "network.parent_interface"),
        ("cluster", "drbd_resource", "../r0", "cluster.drbd_resource"),
        ("cluster", "drbd_resource", "", "cluster.drbd_resource"),
    ],
)
def test_load_settings_rejects_unsafe_resource_tokens(
    monkeypatch, temp_dir, section, key, value, match
):
    config_path = temp_dir / "config.toml"
    config_path.write_text(f"[{section}]\n{key} = {value!r}\n", encoding="utf-8")
    monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))

    from arca_storage.config import load_settings

    with pytest.raises(ValueError, match=match):
        load_settings()


@pytest.mark.unit
@pytest.mark.parametrize(
    "key,value,match",
    [
        ("subprocess_default", 0, "greater than 0"),
        ("pacemaker_operation", -1, "greater than 0"),
        ("nfs_mount", 86401, "less than or equal to 86400"),
    ],
)
def test_load_settings_rejects_invalid_timeouts(
    monkeypatch, temp_dir, key, value, match
):
    config_path = temp_dir / "config.toml"
    config_path.write_text(f"[timeouts]\n{key} = {value}\n", encoding="utf-8")
    monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))

    from arca_storage.config import load_settings

    with pytest.raises(ValueError, match=match):
        load_settings()


@pytest.mark.unit
@pytest.mark.parametrize(
    "tls_lines, match",
    [
        (
            ['ssl_certfile = "/etc/arca-storage/tls/api.crt"'],
            "must be provided together",
        ),
        (
            ['ssl_keyfile = "/etc/arca-storage/tls/api.key"'],
            "must be provided together",
        ),
        (
            [
                'ssl_certfile = "api.crt"',
                'ssl_keyfile = "/etc/arca-storage/tls/api.key"',
            ],
            "api.ssl_certfile",
        ),
        (
            [
                'ssl_certfile = "/etc/arca-storage/tls/api.crt"',
                'ssl_keyfile = "../api.key"',
            ],
            "api.ssl_keyfile",
        ),
    ],
)
def test_load_settings_rejects_invalid_api_tls_paths(
    monkeypatch, temp_dir, tls_lines, match
):
    config_path = temp_dir / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[api]",
                *tls_lines,
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))

    from arca_storage.config import load_settings

    with pytest.raises(ValueError, match=match):
        load_settings()


@pytest.mark.unit
@pytest.mark.parametrize(
    "section,key,value,match",
    [
        ("state", "db_path", "state.db", "absolute POSIX path"),
        ("state", "runtime_dir", "../runtime", "absolute POSIX path"),
        ("state", "runtime_dir", "/var/../runtime", "relative path segments"),
        ("state", "runtime_dir", "/", "filesystem root"),
        ("ganesha", "config_dir", "ganesha", "absolute POSIX path"),
        ("ganesha", "export_dir", "/exports/../escape", "relative path segments"),
        ("ganesha", "export_dir", "/", "filesystem root"),
    ],
)
def test_load_settings_rejects_unsafe_filesystem_paths(
    monkeypatch, temp_dir, section, key, value, match
):
    config_path = temp_dir / "config.toml"
    config_path.write_text(f"[{section}]\n{key} = {value!r}\n", encoding="utf-8")
    monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))

    from arca_storage.config import load_settings

    with pytest.raises(ValueError, match=match):
        load_settings()


@pytest.mark.unit
def test_load_settings_rejects_control_character_filesystem_paths(
    monkeypatch, temp_dir
):
    config_path = temp_dir / "config.toml"
    config_path.write_text(
        '[ganesha]\nconfig_dir = "/srv/ganesha\\nARCA_EXPORT_DIR=/tmp"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))

    from arca_storage.config import load_settings

    with pytest.raises(ValueError, match="control characters"):
        load_settings()


@pytest.mark.unit
def test_load_settings_rejects_invalid_operation_log_retention(monkeypatch, temp_dir):
    config_path = temp_dir / "config.toml"
    config_path.write_text(
        "[state]\noperation_log_retention_days = 0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))

    from arca_storage.config import load_settings

    with pytest.raises(ValueError, match="operation_log_retention_days"):
        load_settings()


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
def test_reconciler_config_type_contract():
    from typing import get_type_hints

    from arca_storage.config import ArcaSettings, ReconcilerConfig

    assert (
        get_type_hints(ArcaSettings.to_reconciler_config)["return"] is ReconcilerConfig
    )
    assert set(get_type_hints(ReconcilerConfig)) == {
        "vg_name",
        "thinpool_name",
        "parent_if",
        "export_dir",
        "drbd_resource",
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


@pytest.mark.unit
def test_systemd_env_quotes_values_for_environment_file():
    from arca_storage import config as config_mod

    settings = config_mod.ArcaSettings(
        ganesha=config_mod.GaneshaConfig(config_dir='/srv/ganesha config "blue"')
    )

    rendered = settings.to_systemd_env()
    assert 'ARCA_GANESHA_CONFIG_DIR="/srv/ganesha config \\"blue\\""' in rendered
