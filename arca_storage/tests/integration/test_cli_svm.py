"""
Integration tests for CLI SVM commands.
"""

import json

import pytest
from typer.testing import CliRunner

from arca_storage.cli.cli import app
from arca_storage.models.base import Phase
from arca_storage.models.svm import SVM, SVMSpec
from .helpers import cli_output


class TestSVMCreate:
    """Tests for svm create command."""

    @pytest.mark.integration
    def test_create_svm_success(self, fake_context):
        """Test successful SVM creation."""
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "svm",
                "create",
                "tenant_a",
                "--vlan",
                "100",
                "--ip",
                "192.168.10.5/24",
                "--gateway",
                "192.168.10.1",
            ],
        )

        assert result.exit_code == 0
        assert "Creating SVM: tenant_a" in result.stdout
        assert fake_context.adapters.netns.namespace_exists("tenant_a") is False
        assert fake_context.adapters.pacemaker.resource_exists("netns_tenant_a")
        assert fake_context.adapters.pacemaker.resource_exists("g_svm_tenant_a")

    @pytest.mark.integration
    def test_create_svm_invalid_name(self):
        """Test creating SVM with invalid name."""
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "svm",
                "create",
                "tenant a",
                "--vlan",
                "100",
                "--ip",
                "192.168.10.5/24",
            ],  # space in name
        )

        assert result.exit_code == 1
        assert "Error" in cli_output(result)

    @pytest.mark.integration
    def test_create_svm_invalid_vlan(self):
        """Test creating SVM with invalid VLAN ID."""
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "svm",
                "create",
                "tenant_a",
                "--vlan",
                "5000",
                "--ip",
                "192.168.10.5/24",
            ],  # invalid VLAN ID
        )

        assert result.exit_code == 1
        assert "Error" in cli_output(result)

    @pytest.mark.integration
    def test_create_svm_invalid_ip(self):
        """Test creating SVM with invalid IP."""
        runner = CliRunner()
        result = runner.invoke(
            app, ["svm", "create", "tenant_a", "--vlan", "100", "--ip", "invalid-ip"]
        )  # invalid IP

        assert result.exit_code == 1
        assert "Error" in cli_output(result)

    @pytest.mark.integration
    def test_create_svm_without_vlan(self, fake_context):
        """Test SVM creation without a VLAN."""
        runner = CliRunner()
        result = runner.invoke(
            app, ["svm", "create", "tenant_a", "--ip", "192.168.10.5/32"]
        )

        assert result.exit_code == 0
        assert fake_context.adapters.netns.namespace_exists("tenant_a") is False
        assert fake_context.adapters.ganesha.host_network["tenant_a"] is True


class TestSVMDelete:
    """Tests for svm delete command."""

    @pytest.mark.integration
    def test_delete_svm_success(self, fake_context):
        """Test successful SVM deletion."""
        runner = CliRunner()
        # First create an SVM
        runner.invoke(
            app,
            [
                "svm",
                "create",
                "tenant_a",
                "--vlan",
                "100",
                "--ip",
                "192.168.10.5/24",
                "--gateway",
                "192.168.10.1",
            ],
        )

        result = runner.invoke(app, ["svm", "delete", "tenant_a"])

        assert result.exit_code == 0
        assert "Deleting SVM: tenant_a" in result.stdout
        assert not fake_context.adapters.netns.namespace_exists("tenant_a")

    @pytest.mark.integration
    def test_delete_svm_force(self, fake_context):
        """Test deleting SVM with force flag."""
        runner = CliRunner()
        # First create an SVM
        runner.invoke(
            app,
            [
                "svm",
                "create",
                "tenant_a",
                "--vlan",
                "100",
                "--ip",
                "192.168.10.5/24",
                "--gateway",
                "192.168.10.1",
            ],
        )

        result = runner.invoke(app, ["svm", "delete", "tenant_a", "--force"])

        assert result.exit_code == 0


class TestSVMList:
    """Tests for svm list command."""

    @pytest.mark.integration
    def test_list_svms(self, fake_context):
        """Test listing SVMs."""
        runner = CliRunner()
        result = runner.invoke(app, ["svm", "list"])

        assert result.exit_code == 0

    @pytest.mark.integration
    def test_list_svms_paginates_all_records(self, fake_context):
        """List all SVMs, not only the DB default first page."""
        for i in range(105):
            svm = SVM(
                spec=SVMSpec(name=f"tenant_{i:03d}", ip_cidr=f"10.0.0.{i + 1}/32")
            )
            svm.status.phase = Phase.READY
            fake_context.db.insert_svm(svm)

        runner = CliRunner()
        result = runner.invoke(app, ["svm", "list"])

        assert result.exit_code == 0
        assert "tenant_000" in result.stdout
        assert "tenant_104" in result.stdout
        assert result.stdout.count("tenant_") == 105

    @pytest.mark.integration
    def test_list_svms_supports_json_cursor_page(self, fake_context):
        """List an explicit SVM page as JSON with a follow-up cursor."""
        for i in range(3):
            svm = SVM(
                spec=SVMSpec(name=f"tenant_{i:03d}", ip_cidr=f"10.0.0.{i + 1}/32")
            )
            svm.status.phase = Phase.READY
            fake_context.db.insert_svm(svm)

        runner = CliRunner()
        result = runner.invoke(app, ["svm", "list", "--limit", "2", "--format", "json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert [item["spec"]["name"] for item in payload["items"]] == [
            "tenant_000",
            "tenant_001",
        ]
        assert payload["next_cursor"]

        result = runner.invoke(
            app, ["svm", "list", "--cursor", payload["next_cursor"], "--format", "json"]
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert [item["spec"]["name"] for item in payload["items"]] == ["tenant_002"]
        assert payload["next_cursor"] is None
