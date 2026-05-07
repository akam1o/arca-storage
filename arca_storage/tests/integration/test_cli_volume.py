"""
Integration tests for CLI volume commands.
"""

import pytest
from typer.testing import CliRunner

from arca_storage.cli.cli import app


def create_test_svm(runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["svm", "create", "tenant_a", "--vlan", "100", "--ip", "192.168.10.5/24", "--gateway", "192.168.10.1"],
    )
    assert result.exit_code == 0


class TestVolumeCreate:
    """Tests for volume create command."""

    @pytest.mark.integration
    def test_create_volume_success(self, fake_context):
        """Test successful volume creation."""
        runner = CliRunner()
        create_test_svm(runner)
        result = runner.invoke(app, ["volume", "create", "vol1", "--svm", "tenant_a", "--size", "100"])

        assert result.exit_code == 0
        assert "Creating volume: vol1" in result.stdout
        assert fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_vol1")

    @pytest.mark.integration
    def test_create_volume_no_thin(self, fake_context):
        """Test creating volume without thin provisioning."""
        runner = CliRunner()
        create_test_svm(runner)
        result = runner.invoke(app, ["volume", "create", "vol1", "--svm", "tenant_a", "--size", "100", "--no-thin"])

        assert result.exit_code == 0
        assert fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_vol1")


class TestVolumeResize:
    """Tests for volume resize command."""

    @pytest.mark.integration
    def test_resize_volume_success(self, fake_context):
        """Test successful volume resize."""
        runner = CliRunner()
        create_test_svm(runner)
        runner.invoke(app, ["volume", "create", "vol1", "--svm", "tenant_a", "--size", "100"])

        result = runner.invoke(app, ["volume", "resize", "vol1", "--svm", "tenant_a", "--new-size", "200"])

        assert result.exit_code == 0
        assert "Resizing volume: vol1" in result.stdout


class TestVolumeDelete:
    """Tests for volume delete command."""

    @pytest.mark.integration
    def test_delete_volume_success(self, fake_context):
        """Test successful volume deletion."""
        runner = CliRunner()
        create_test_svm(runner)
        runner.invoke(app, ["volume", "create", "vol1", "--svm", "tenant_a", "--size", "100"])

        result = runner.invoke(app, ["volume", "delete", "vol1", "--svm", "tenant_a"])

        assert result.exit_code == 0
        assert "Deleting volume: vol1" in result.stdout
        assert not fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_vol1")
