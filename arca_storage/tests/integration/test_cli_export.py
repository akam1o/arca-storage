"""
Integration tests for CLI export commands.
"""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from arca_storage.cli.cli import app


class TestExportAdd:
    """Tests for export add command."""

    @pytest.mark.integration
    @patch("arca_storage.cli.commands.export.add_export")
    @patch("arca_storage.cli.commands.export.reload_ganesha")
    def test_add_export_success(self, mock_reload, mock_add):
        """Test successful export addition."""
        runner = CliRunner()
        result = runner.invoke(
            app, ["export", "add", "--volume", "vol1", "--svm", "tenant_a", "--client", "10.0.0.0/24", "--access", "rw"]
        )

        assert result.exit_code == 0
        assert "Adding export" in result.stdout
        mock_add.assert_called_once()
        mock_reload.assert_called_once_with("tenant_a")

    @pytest.mark.integration
    def test_add_export_invalid_client(self):
        """Test adding export with invalid client CIDR."""
        runner = CliRunner()
        result = runner.invoke(
            app, ["export", "add", "--volume", "vol1", "--svm", "tenant_a", "--client", "invalid-cidr"]
        )

        assert result.exit_code == 1
        assert "Error" in result.stdout


class TestExportRemove:
    """Tests for export remove command."""

    @pytest.mark.integration
    @patch("arca_storage.cli.commands.export.remove_export")
    @patch("arca_storage.cli.commands.export.reload_ganesha")
    def test_remove_export_success(self, mock_reload, mock_remove):
        """Test successful export removal."""
        runner = CliRunner()
        result = runner.invoke(
            app, ["export", "remove", "--volume", "vol1", "--svm", "tenant_a", "--client", "10.0.0.0/24"]
        )

        assert result.exit_code == 0
        assert "Removing export" in result.stdout
        mock_remove.assert_called_once()
        mock_reload.assert_called_once_with("tenant_a")
