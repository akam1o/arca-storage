"""
Integration tests for CLI export commands.
"""

import pytest
from typer.testing import CliRunner

from arca_storage.cli.cli import app
from .helpers import cli_output


def create_test_volume(runner: CliRunner) -> None:
    svm = runner.invoke(
        app,
        ["svm", "create", "tenant_a", "--vlan", "100", "--ip", "192.168.10.5/24", "--gateway", "192.168.10.1"],
    )
    assert svm.exit_code == 0
    volume = runner.invoke(app, ["volume", "create", "vol1", "--svm", "tenant_a", "--size", "10"])
    assert volume.exit_code == 0


class TestExportAdd:
    """Tests for export add command."""

    @pytest.mark.integration
    def test_add_export_success(self, fake_context):
        """Test successful export addition."""
        runner = CliRunner()
        create_test_volume(runner)
        result = runner.invoke(
            app, ["export", "add", "--volume", "vol1", "--svm", "tenant_a", "--client", "10.0.0.0/24", "--access", "rw"]
        )

        assert result.exit_code == 0
        assert "Adding export" in result.stdout

    @pytest.mark.integration
    def test_add_export_invalid_client(self):
        """Test adding export with invalid client CIDR."""
        runner = CliRunner()
        result = runner.invoke(
            app, ["export", "add", "--volume", "vol1", "--svm", "tenant_a", "--client", "invalid-cidr"]
        )

        assert result.exit_code == 1
        assert "Error" in cli_output(result)


class TestExportRemove:
    """Tests for export remove command."""

    @pytest.mark.integration
    def test_remove_export_success(self, fake_context):
        """Test successful export removal."""
        runner = CliRunner()
        create_test_volume(runner)
        runner.invoke(
            app, ["export", "add", "--volume", "vol1", "--svm", "tenant_a", "--client", "10.0.0.0/24"]
        )

        result = runner.invoke(
            app, ["export", "remove", "--volume", "vol1", "--svm", "tenant_a", "--client", "10.0.0.0/24"]
        )

        assert result.exit_code == 0
        assert "Removing export" in result.stdout
