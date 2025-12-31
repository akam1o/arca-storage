"""
Integration tests for CLI volume commands.
"""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from arca_storage.cli.cli import app


class TestVolumeCreate:
    """Tests for volume create command."""

    @pytest.mark.integration
    @patch("arca_storage.cli.commands.volume.create_lv")
    @patch("arca_storage.cli.commands.volume.format_xfs")
    @patch("arca_storage.cli.commands.volume.mount_xfs")
    def test_create_volume_success(self, mock_mount, mock_format, mock_create_lv):
        """Test successful volume creation."""
        mock_create_lv.return_value = "/dev/vg_pool_01/vol_tenant_a_vol1"

        runner = CliRunner()
        result = runner.invoke(app, ["volume", "create", "vol1", "--svm", "tenant_a", "--size", "100"])

        assert result.exit_code == 0
        assert "Creating volume: vol1" in result.stdout
        mock_create_lv.assert_called_once_with("vg_pool_01", "vol_tenant_a_vol1", 100, thin=True)
        mock_format.assert_called_once()
        mock_mount.assert_called_once()

    @pytest.mark.integration
    @patch("arca_storage.cli.commands.volume.create_lv")
    @patch("arca_storage.cli.commands.volume.format_xfs")
    @patch("arca_storage.cli.commands.volume.mount_xfs")
    def test_create_volume_no_thin(self, mock_mount, mock_format, mock_create_lv):
        """Test creating volume without thin provisioning."""
        mock_create_lv.return_value = "/dev/vg_pool_01/vol_tenant_a_vol1"

        runner = CliRunner()
        result = runner.invoke(app, ["volume", "create", "vol1", "--svm", "tenant_a", "--size", "100", "--no-thin"])

        assert result.exit_code == 0
        mock_create_lv.assert_called_once_with("vg_pool_01", "vol_tenant_a_vol1", 100, thin=False)


class TestVolumeResize:
    """Tests for volume resize command."""

    @pytest.mark.integration
    @patch("arca_storage.cli.commands.volume.resize_lv")
    @patch("arca_storage.cli.commands.volume.grow_xfs")
    def test_resize_volume_success(self, mock_grow, mock_resize):
        """Test successful volume resize."""
        runner = CliRunner()
        result = runner.invoke(app, ["volume", "resize", "vol1", "--svm", "tenant_a", "--new-size", "200"])

        assert result.exit_code == 0
        assert "Resizing volume: vol1" in result.stdout
        mock_resize.assert_called_once_with("vg_pool_01", "vol_tenant_a_vol1", 200)
        mock_grow.assert_called_once()


class TestVolumeDelete:
    """Tests for volume delete command."""

    @pytest.mark.integration
    @patch("arca_storage.cli.commands.volume.umount_xfs")
    @patch("arca_storage.cli.commands.volume.delete_lv")
    def test_delete_volume_success(self, mock_delete, mock_umount):
        """Test successful volume deletion."""
        runner = CliRunner()
        result = runner.invoke(app, ["volume", "delete", "vol1", "--svm", "tenant_a"])

        assert result.exit_code == 0
        assert "Deleting volume: vol1" in result.stdout
        mock_umount.assert_called_once()
        mock_delete.assert_called_once()
