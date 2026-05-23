"""
Integration tests for CLI volume commands.
"""

import json

import pytest
from typer.testing import CliRunner

from arca_storage.cli.cli import app
from arca_storage.cli.lib.validators import volume_lv_name
from arca_storage.models.base import Phase
from arca_storage.models.volume import Volume, VolumeSpec


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
        assert fake_context.adapters.lvm.lv_exists("vg_pool_01", volume_lv_name("tenant_a", "vol1"))

    @pytest.mark.integration
    def test_create_volume_no_thin(self, fake_context):
        """Test creating volume without thin provisioning."""
        runner = CliRunner()
        create_test_svm(runner)
        result = runner.invoke(app, ["volume", "create", "vol1", "--svm", "tenant_a", "--size", "100", "--no-thin"])

        assert result.exit_code == 0
        assert fake_context.adapters.lvm.lv_exists("vg_pool_01", volume_lv_name("tenant_a", "vol1"))


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
        assert not fake_context.adapters.lvm.lv_exists("vg_pool_01", volume_lv_name("tenant_a", "vol1"))


class TestVolumeList:
    """Tests for volume list command."""

    @pytest.mark.integration
    def test_list_volumes_paginates_all_records(self, fake_context):
        """List all volumes, not only the DB default first page."""
        for i in range(105):
            volume = Volume(spec=VolumeSpec(name=f"vol_{i:03d}", svm="tenant_a", size_gib=1))
            volume.status.phase = Phase.READY
            fake_context.db.insert_volume(volume)

        runner = CliRunner()
        result = runner.invoke(app, ["volume", "list", "--svm", "tenant_a"])

        assert result.exit_code == 0
        assert "tenant_a/vol_000" in result.stdout
        assert "tenant_a/vol_104" in result.stdout
        assert result.stdout.count("tenant_a/vol_") == 105

    @pytest.mark.integration
    def test_list_volumes_supports_json_cursor_page(self, fake_context):
        """List an explicit volume page as JSON with a follow-up cursor."""
        for i in range(3):
            volume = Volume(spec=VolumeSpec(name=f"vol_{i:03d}", svm="tenant_a", size_gib=1))
            volume.status.phase = Phase.READY
            fake_context.db.insert_volume(volume)

        runner = CliRunner()
        result = runner.invoke(
            app, ["volume", "list", "--svm", "tenant_a", "--limit", "2", "--format", "json"]
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert [item["spec"]["name"] for item in payload["items"]] == [
            "vol_000",
            "vol_001",
        ]
        assert payload["next_cursor"]

        result = runner.invoke(
            app,
            [
                "volume",
                "list",
                "--svm",
                "tenant_a",
                "--cursor",
                payload["next_cursor"],
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert [item["spec"]["name"] for item in payload["items"]] == ["vol_002"]
        assert payload["next_cursor"] is None
