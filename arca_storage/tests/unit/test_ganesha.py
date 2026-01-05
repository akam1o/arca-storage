"""
Unit tests for ganesha module.
"""

from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from arca_storage.cli.lib.ganesha import add_export, reload, remove_export, render_config, sync


class TestRenderConfig:
    """Tests for render_config function."""

    @pytest.mark.unit
    @patch("pathlib.Path.mkdir")
    @patch("builtins.open", new_callable=mock_open)
    def test_render_config_empty_exports(self, mock_file, mock_mkdir):
        """Test rendering config with no exports."""
        result = render_config("tenant_a", [])

        assert result == "/etc/ganesha/ganesha.tenant_a.conf"
        assert mock_mkdir.called
        assert mock_file.call_count >= 1

    @pytest.mark.unit
    @patch("pathlib.Path.mkdir")
    @patch("builtins.open", new_callable=mock_open)
    def test_render_config_with_exports(self, mock_file, mock_mkdir):
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

        assert result == "/etc/ganesha/ganesha.tenant_a.conf"
        # Verify file was written
        assert mock_file().write.call_count >= 1


class TestReload:
    """Tests for reload function."""

    @pytest.mark.unit
    def test_reload_success(self, mock_subprocess):
        """Test successful reload."""
        mock_subprocess.return_value = MagicMock(returncode=0)

        reload("tenant_a")

        mock_subprocess.assert_called_once_with(
            ["systemctl", "reload", "nfs-ganesha@tenant_a"], capture_output=True, text=True, check=False
        )

    @pytest.mark.unit
    def test_reload_fails(self, mock_subprocess):
        """Test reload fails."""
        mock_subprocess.return_value = MagicMock(returncode=1, stderr="Error")

        with pytest.raises(RuntimeError, match="Failed to reload NFS-Ganesha"):
            reload("tenant_a")


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
    def test_add_export_increments_id(self, mock_reload, mock_render, mock_save, mock_load):
        """Test export ID is incremented."""
        mock_load.return_value = [{"export_id": 101, "path": "/exports/tenant_a/vol1", "client": "10.0.0.0/24"}]

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
        mock_load.return_value = [{"export_id": 101, "path": "/exports/tenant_a/vol1", "client": "10.0.0.0/24"}]

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
    def test_remove_nonexistent_export(self, mock_reload, mock_render, mock_save, mock_load):
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
