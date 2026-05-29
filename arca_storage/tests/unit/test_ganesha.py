"""
Unit tests for ganesha module.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from arca_storage.adapters.ganesha import SubprocessGaneshaAdapter
from arca_storage.cli.lib import ganesha
from arca_storage.cli.lib.ganesha import (
    add_export,
    reload,
    remove_export,
    render_config,
    sync,
)
from arca_storage.config import ArcaSettings, GaneshaConfig, StateConfig


def _assert_redacted(error: BaseException, *values: str) -> None:
    rendered = str(error)
    for value in values:
        assert value not in rendered


@pytest.fixture(autouse=True)
def arca_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[state]\nruntime_dir = "{tmp_path}"\n'
        f'[ganesha]\nconfig_dir = "{tmp_path / "ganesha"}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))


class TestRenderConfig:
    """Tests for render_config function."""

    @pytest.mark.unit
    def test_render_config_empty_exports(self, tmp_path):
        """Test rendering config with no exports."""
        result = render_config("tenant_a", [])

        assert result == str(tmp_path / "ganesha" / "ganesha.tenant_a.conf")
        assert Path(result).exists()

    @pytest.mark.unit
    def test_render_config_with_exports(self):
        """Test rendering config with exports."""
        exports = [
            {
                "export_id": 101,
                "path": "/exports/tenant_a/vol1",
                "pseudo": "/exports/tenant_a/vol1",
                "access": "RW",
                "squash": "Root_Squash",
                "sec": ["sys"],
                "client": "10.0.0.0/24",
            }
        ]

        result = render_config("tenant_a", exports)

        content = Path(result).read_text(encoding="utf-8")
        assert "Export_Id = 101;" in content
        assert 'Clients = "10.0.0.0/24";' in content

    @pytest.mark.unit
    @pytest.mark.parametrize("field", ["path", "pseudo", "client"])
    def test_render_config_rejects_unsafe_quoted_fields(self, field):
        exports = [
            {
                "export_id": 101,
                "path": "/exports/tenant_a/vol1",
                "pseudo": "/exports/tenant_a/vol1",
                "access": "RW",
                "squash": "Root_Squash",
                "sec": ["sys"],
                "client": "10.0.0.0/24",
            }
        ]
        exports[0][field] = 'bad"\nvalue'

        with pytest.raises(ValueError, match="Unsafe Ganesha"):
            render_config("tenant_a", exports)

    @pytest.mark.unit
    def test_render_config_rejects_unsafe_tokens(self):
        exports = [
            {
                "export_id": 101,
                "path": "/exports/tenant_a/vol1",
                "pseudo": "/exports/tenant_a/vol1",
                "access": "RW;\nCLIENT",
                "squash": "Root_Squash",
                "sec": ["sys"],
                "client": "10.0.0.0/24",
            }
        ]

        with pytest.raises(ValueError, match="Unsupported Ganesha Access_Type"):
            render_config("tenant_a", exports)

    @pytest.mark.unit
    def test_write_if_changed_keeps_existing_file_on_replace_failure(
        self, monkeypatch, tmp_path
    ):
        target = tmp_path / "ganesha.conf"
        target.write_text("old config", encoding="utf-8")

        def fail_replace(src, dst):
            assert Path(src).read_text(encoding="utf-8") == "new config"
            raise OSError("replace failed")

        monkeypatch.setattr(ganesha.os, "replace", fail_replace)

        with pytest.raises(OSError, match="replace failed"):
            ganesha._write_if_changed(target, "new config")

        assert target.read_text(encoding="utf-8") == "old config"
        assert list(tmp_path.glob(".ganesha.conf.*")) == []

    @pytest.mark.unit
    def test_render_config_with_bind_addr(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'[state]\nruntime_dir = "{tmp_path}"\n'
            f'[ganesha]\nconfig_dir = "{tmp_path / "ganesha"}"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))

        result = render_config("tenant_a", [], bind_addr="192.168.10.5")

        content = Path(result).read_text(encoding="utf-8")
        assert "Bind_addr = 192.168.10.5;" in content

    @pytest.mark.unit
    def test_subprocess_adapter_uses_injected_settings(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ARCA_CONFIG_PATH", raising=False)
        settings = ArcaSettings(
            state=StateConfig(runtime_dir=str(tmp_path / "state")),
            ganesha=GaneshaConfig(config_dir=str(tmp_path / "custom-ganesha")),
        )
        adapter = SubprocessGaneshaAdapter(settings=settings)

        result = adapter.render_config("tenant_injected", [])

        assert result == str(
            tmp_path / "custom-ganesha" / "ganesha.tenant_injected.conf"
        )
        assert (tmp_path / "custom-ganesha" / "ganesha.tenant_injected.conf").exists()

    @pytest.mark.unit
    def test_render_config_rejects_unsafe_svm_name(self):
        with pytest.raises(ValueError, match="Name must"):
            render_config("../tenant_a", [])


class TestConfigSnapshots:
    @pytest.mark.unit
    def test_snapshot_meta_reads_rendered_version_and_latest(self):
        render_config("tenant_a", [])

        latest = ganesha.read_config_snapshot_meta("tenant_a", "latest")
        version = latest["config_version"]

        assert (
            ganesha.read_config_snapshot_meta("tenant_a", version)["config_version"]
            == version
        )
        assert any(
            s["config_version"] == version
            for s in ganesha.list_config_snapshots("tenant_a")
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "config_version",
        [
            "../outside",
            "abc/def123456",
            "ABCDEF123456",
            "short",
            "",
        ],
    )
    def test_rollback_rejects_unsafe_config_version(self, config_version):
        with pytest.raises(ValueError, match="config_version"):
            ganesha.rollback_config("tenant_a", config_version)

    @pytest.mark.unit
    def test_snapshot_meta_rejects_unsafe_config_version(self):
        with pytest.raises(ValueError, match="config_version"):
            ganesha.read_config_snapshot_meta("tenant_a", "../outside")

    @pytest.mark.unit
    def test_list_config_snapshots_ignores_untrusted_versions(self, tmp_path):
        snapshot_dir = tmp_path / "config"
        snapshot_dir.mkdir()
        (snapshot_dir / "ganesha.tenant_a.abcdef123456.conf").write_text(
            "valid", encoding="utf-8"
        )
        (snapshot_dir / "ganesha.tenant_a.not-a-digest.conf").write_text(
            "invalid", encoding="utf-8"
        )
        (snapshot_dir / "ganesha.tenant_a.latest.conf").write_text(
            "latest", encoding="utf-8"
        )

        snapshots = ganesha.list_config_snapshots("tenant_a")

        assert [snapshot["config_version"] for snapshot in snapshots] == [
            "abcdef123456"
        ]


class TestReload:
    """Tests for reload function."""

    @pytest.mark.unit
    def test_reload_success(self, mock_subprocess):
        """Test successful reload."""
        mock_subprocess.return_value = MagicMock(returncode=0)

        reload("tenant_a")

        mock_subprocess.assert_called_once_with(
            ["systemctl", "reload", "nfs-ganesha@tenant_a"],
            capture_output=True,
            text=True,
            check=False,
            timeout=ganesha._DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )

    @pytest.mark.unit
    def test_reload_host_network(self, mock_subprocess):
        """Test reloading host-namespace unit."""
        mock_subprocess.return_value = MagicMock(returncode=0)

        reload("tenant_a", host_network=True)

        mock_subprocess.assert_called_once_with(
            ["systemctl", "reload", "nfs-ganesha-host@tenant_a"],
            capture_output=True,
            text=True,
            check=False,
            timeout=ganesha._DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )

    @pytest.mark.unit
    def test_reload_fails(self, mock_subprocess):
        """Test reload fails."""
        mock_subprocess.return_value = MagicMock(
            returncode=1, stderr="secret-token tenant_a"
        )

        with pytest.raises(
            RuntimeError, match="Failed to reload NFS-Ganesha"
        ) as exc_info:
            reload("tenant_a")

        _assert_redacted(exc_info.value, "secret-token", "tenant_a")


class TestAddExport:
    """Tests for add_export function."""

    @pytest.mark.unit
    @patch("arca_storage.cli.lib.ganesha._load_exports")
    @patch("arca_storage.cli.lib.ganesha._save_exports")
    @patch("arca_storage.cli.lib.ganesha.render_config")
    @patch("arca_storage.cli.lib.ganesha.reload")
    def test_add_export_new(self, mock_reload, mock_render, mock_save, mock_load):
        """Test adding a new export."""
        mock_load.return_value = []

        add_export("tenant_a", "vol1", "10.0.0.0/24", "rw", True)

        mock_load.assert_called_once_with("tenant_a")
        mock_save.assert_called_once()
        mock_render.assert_called_once()
        mock_reload.assert_called_once_with("tenant_a")

    @pytest.mark.unit
    @patch("arca_storage.cli.lib.ganesha._load_exports")
    @patch("arca_storage.cli.lib.ganesha._save_exports")
    @patch("arca_storage.cli.lib.ganesha.render_config")
    @patch("arca_storage.cli.lib.ganesha.reload")
    def test_add_export_increments_id(
        self, mock_reload, mock_render, mock_save, mock_load
    ):
        """Test export ID is incremented."""
        mock_load.return_value = [
            {
                "export_id": 101,
                "path": "/exports/tenant_a/vol1",
                "client": "10.0.0.0/24",
            }
        ]

        add_export("tenant_a", "vol2", "10.0.0.0/24", "rw", True)

        # Verify export_id is 102
        call_args = mock_save.call_args[0]
        exports = call_args[1]
        assert exports[-1]["export_id"] == 102


class TestRemoveExport:
    """Tests for remove_export function."""

    @pytest.mark.unit
    @patch("arca_storage.cli.lib.ganesha._load_exports")
    @patch("arca_storage.cli.lib.ganesha._save_exports")
    @patch("arca_storage.cli.lib.ganesha.render_config")
    @patch("arca_storage.cli.lib.ganesha.reload")
    def test_remove_export(self, mock_reload, mock_render, mock_save, mock_load):
        """Test removing an export."""
        mock_load.return_value = [
            {
                "export_id": 101,
                "path": "/exports/tenant_a/vol1",
                "client": "10.0.0.0/24",
            }
        ]

        remove_export("tenant_a", "vol1", "10.0.0.0/24")

        # Verify export was removed
        call_args = mock_save.call_args[0]
        exports = call_args[1]
        assert len(exports) == 0
        mock_reload.assert_called_once_with("tenant_a")

    @pytest.mark.unit
    @patch("arca_storage.cli.lib.ganesha._load_exports")
    @patch("arca_storage.cli.lib.ganesha._save_exports")
    @patch("arca_storage.cli.lib.ganesha.render_config")
    @patch("arca_storage.cli.lib.ganesha.reload")
    def test_remove_nonexistent_export(
        self, mock_reload, mock_render, mock_save, mock_load
    ):
        """Test removing export that doesn't exist."""
        mock_load.return_value = []

        # Should not raise error, just remove nothing
        remove_export("tenant_a", "vol1", "10.0.0.0/24")

        mock_reload.assert_called_once_with("tenant_a")


class TestSync:
    @pytest.mark.unit
    @patch("arca_storage.cli.lib.ganesha._load_exports")
    @patch("arca_storage.cli.lib.ganesha.render_config")
    @patch("arca_storage.cli.lib.ganesha.reload")
    def test_sync_renders_and_reloads(self, mock_reload, mock_render, mock_load):
        mock_load.return_value = []
        mock_render.return_value = "/etc/ganesha/ganesha.tenant_a.conf"

        path = sync("tenant_a")

        assert path == "/etc/ganesha/ganesha.tenant_a.conf"
        mock_load.assert_called_once_with("tenant_a")
        mock_render.assert_called_once_with("tenant_a", [])
        mock_reload.assert_called_once_with("tenant_a")
