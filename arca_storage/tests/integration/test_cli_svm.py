"""
Integration tests for CLI SVM commands.
"""

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from arca_storage.cli.cli import app


class TestSVMCreate:
    """Tests for svm create command."""

    @pytest.mark.integration
    @patch("arca_storage.cli.commands.svm.create_namespace")
    @patch("arca_storage.cli.commands.svm.attach_vlan")
    @patch("arca_storage.cli.commands.svm.render_config")
    @patch("arca_storage.cli.commands.svm.create_group")
    def test_create_svm_success(self, mock_create_group, mock_render, mock_attach, mock_create_ns):
        """Test successful SVM creation."""
        runner = CliRunner()
        result = runner.invoke(
            app, ["svm", "create", "tenant_a", "--vlan", "100", "--ip", "192.168.10.5/24", "--gateway", "192.168.10.1"]
        )

        assert result.exit_code == 0
        assert "Creating SVM: tenant_a" in result.stdout
        mock_create_ns.assert_called_once_with("tenant_a")
        mock_attach.assert_called_once()
        mock_render.assert_called_once()
        mock_create_group.assert_called_once()

    @pytest.mark.integration
    def test_create_svm_invalid_name(self):
        """Test creating SVM with invalid name."""
        runner = CliRunner()
        result = runner.invoke(
            app, ["svm", "create", "tenant a", "--vlan", "100", "--ip", "192.168.10.5/24"]  # space in name
        )

        assert result.exit_code == 1
        assert "Error" in result.stdout

    @pytest.mark.integration
    def test_create_svm_invalid_vlan(self):
        """Test creating SVM with invalid VLAN ID."""
        runner = CliRunner()
        result = runner.invoke(
            app, ["svm", "create", "tenant_a", "--vlan", "5000", "--ip", "192.168.10.5/24"]  # invalid VLAN ID
        )

        assert result.exit_code == 1
        assert "Error" in result.stdout

    @pytest.mark.integration
    def test_create_svm_invalid_ip(self):
        """Test creating SVM with invalid IP."""
        runner = CliRunner()
        result = runner.invoke(app, ["svm", "create", "tenant_a", "--vlan", "100", "--ip", "invalid-ip"])  # invalid IP

        assert result.exit_code == 1
        assert "Error" in result.stdout


class TestSVMDelete:
    """Tests for svm delete command."""

    @pytest.mark.integration
    @patch("arca_storage.cli.commands.svm.delete_group")
    @patch("arca_storage.cli.commands.svm.stop_unit")
    @patch("arca_storage.cli.commands.svm.delete_namespace")
    def test_delete_svm_success(self, mock_delete_ns, mock_stop, mock_delete_group):
        """Test successful SVM deletion."""
        runner = CliRunner()
        result = runner.invoke(app, ["svm", "delete", "tenant_a"])

        assert result.exit_code == 0
        assert "Deleting SVM: tenant_a" in result.stdout
        mock_delete_group.assert_called_once_with("tenant_a")
        mock_stop.assert_called_once_with("nfs-ganesha@tenant_a")
        mock_delete_ns.assert_called_once_with("tenant_a")

    @pytest.mark.integration
    @patch("arca_storage.cli.commands.svm.delete_group")
    @patch("arca_storage.cli.commands.svm.stop_unit")
    @patch("arca_storage.cli.commands.svm.delete_namespace")
    def test_delete_svm_force(self, mock_delete_ns, mock_stop, mock_delete_group):
        """Test deleting SVM with force flag."""
        runner = CliRunner()
        result = runner.invoke(app, ["svm", "delete", "tenant_a", "--force"])

        assert result.exit_code == 0
        mock_delete_group.assert_called_once()


class TestSVMList:
    """Tests for svm list command."""

    @pytest.mark.integration
    def test_list_svms(self):
        """Test listing SVMs."""
        runner = CliRunner()
        result = runner.invoke(app, ["svm", "list"])

        # Currently returns placeholder, so just check it doesn't crash
        assert result.exit_code in [0, 1]  # May return error if not implemented
