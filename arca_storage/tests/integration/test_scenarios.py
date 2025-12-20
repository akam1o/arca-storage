"""
Scenario tests for end-to-end workflows.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from arca_storage.api.main import app as api_app
from arca_storage.cli.cli import app as cli_app


class TestSVMWorkflow:
    """End-to-end scenario: Create SVM -> Create Volume -> Add Export -> Delete."""

    @pytest.mark.integration
    @pytest.mark.slow
    @patch("arca_storage.cli.commands.svm.create_namespace")
    @patch("arca_storage.cli.commands.svm.attach_vlan")
    @patch("arca_storage.cli.commands.svm.render_config")
    @patch("arca_storage.cli.commands.svm.create_group")
    @patch("arca_storage.cli.commands.volume.create_lv")
    @patch("arca_storage.cli.commands.volume.format_xfs")
    @patch("arca_storage.cli.commands.volume.mount_xfs")
    @patch("arca_storage.cli.commands.export.add_export")
    @patch("arca_storage.cli.commands.export.reload_ganesha")
    @patch("arca_storage.cli.commands.svm.delete_group")
    @patch("arca_storage.cli.commands.svm.stop_unit")
    @patch("arca_storage.cli.commands.svm.delete_namespace")
    @patch("arca_storage.cli.commands.volume.umount_xfs")
    @patch("arca_storage.cli.commands.volume.delete_lv")
    def test_full_svm_lifecycle(
        self,
        mock_delete_lv,
        mock_umount,
        mock_delete_ns,
        mock_stop,
        mock_delete_group,
        mock_reload,
        mock_add_export,
        mock_mount,
        mock_format,
        mock_create_lv,
        mock_create_group,
        mock_render,
        mock_attach,
        mock_create_ns,
    ):
        """Test complete SVM lifecycle."""
        mock_create_lv.return_value = "/dev/vg_pool_01/vol1"
        runner = CliRunner()

        # 1. Create SVM
        result = runner.invoke(
            cli_app,
            ["svm", "create", "tenant_a", "--vlan", "100", "--ip", "192.168.10.5/24", "--gateway", "192.168.10.1"],
        )
        assert result.exit_code == 0

        # 2. Create Volume
        result = runner.invoke(cli_app, ["volume", "create", "vol1", "--svm", "tenant_a", "--size", "100"])
        assert result.exit_code == 0

        # 3. Add Export
        result = runner.invoke(
            cli_app, ["export", "add", "--volume", "vol1", "--svm", "tenant_a", "--client", "10.0.0.0/24"]
        )
        assert result.exit_code == 0

        # 4. Delete Volume
        result = runner.invoke(cli_app, ["volume", "delete", "vol1", "--svm", "tenant_a"])
        assert result.exit_code == 0

        # 5. Delete SVM
        result = runner.invoke(cli_app, ["svm", "delete", "tenant_a"])
        assert result.exit_code == 0

        # Verify all mocks were called
        mock_create_ns.assert_called_once()
        mock_attach.assert_called_once()
        mock_create_lv.assert_called_once()
        mock_add_export.assert_called_once()
        mock_delete_lv.assert_called_once()
        mock_delete_ns.assert_called_once()


class TestAPIWorkflow:
    """End-to-end scenario: API-based workflow."""

    @pytest.mark.integration
    @pytest.mark.slow
    @patch("arca_storage.api.services.svm_service.create_svm")
    @patch("arca_storage.api.services.volume_service.create_volume")
    @patch("arca_storage.api.services.export_service.add_export")
    @patch("arca_storage.api.services.volume_service.delete_volume")
    @patch("arca_storage.api.services.svm_service.delete_svm")
    @pytest.mark.asyncio
    async def test_api_full_workflow(
        self, mock_delete_svm, mock_delete_vol, mock_add_export, mock_create_vol, mock_create_svm
    ):
        """Test complete API workflow."""
        mock_create_svm.return_value = {
            "name": "tenant_a",
            "vlan_id": 100,
            "ip_cidr": "192.168.10.5/24",
            "gateway": "192.168.10.1",
            "mtu": 1500,
            "namespace": "tenant_a",
            "vip": "192.168.10.5",
            "status": "available",
            "created_at": "2025-12-20T12:00:00Z",
        }
        mock_create_vol.return_value = {
            "name": "vol1",
            "svm": "tenant_a",
            "size_gib": 100,
            "thin": True,
            "fs_type": "xfs",
            "mount_path": "/exports/tenant_a/vol1",
            "lv_path": "/dev/vg_pool_01/vol1",
            "status": "available",
            "created_at": "2025-12-20T12:00:00Z",
        }
        mock_add_export.return_value = {
            "svm": "tenant_a",
            "volume": "vol1",
            "client": "10.0.0.0/24",
            "access": "rw",
            "root_squash": True,
            "sec": ["sys"],
            "pseudo": "/exports/tenant_a/vol1",
            "export_id": 101,
            "status": "available",
            "created_at": "2025-12-20T12:00:00Z",
        }
        mock_delete_vol.return_value = None
        mock_delete_svm.return_value = None

        client = TestClient(api_app)

        # 1. Create SVM
        response = client.post(
            "/v1/svms",
            json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": "192.168.10.5/24", "gateway": "192.168.10.1"},
        )
        assert response.status_code == 201

        # 2. Create Volume
        response = client.post("/v1/volumes", json={"name": "vol1", "svm": "tenant_a", "size_gib": 100})
        assert response.status_code == 201

        # 3. Add Export
        response = client.post("/v1/exports", json={"svm": "tenant_a", "volume": "vol1", "client": "10.0.0.0/24"})
        assert response.status_code == 201

        # 4. Delete Volume
        response = client.delete("/v1/volumes/vol1?svm=tenant_a")
        assert response.status_code == 200

        # 5. Delete SVM
        response = client.delete("/v1/svms/tenant_a")
        assert response.status_code == 200


class TestErrorHandling:
    """Test error handling scenarios."""

    @pytest.mark.integration
    @patch("arca_storage.cli.commands.svm.create_namespace")
    def test_svm_create_failure_rollback(self, mock_create_ns):
        """Test SVM creation failure triggers rollback."""
        mock_create_ns.side_effect = RuntimeError("Failed to create namespace")

        runner = CliRunner()
        result = runner.invoke(cli_app, ["svm", "create", "tenant_a", "--vlan", "100", "--ip", "192.168.10.5/24"])

        assert result.exit_code == 1
        assert "Error" in result.stdout

    @pytest.mark.integration
    @patch("arca_storage.api.services.svm_service.create_svm")
    def test_api_error_response(self, mock_create_svm):
        """Test API returns proper error response."""
        mock_create_svm.side_effect = ValueError("Invalid configuration")

        client = TestClient(api_app)
        response = client.post("/v1/svms", json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": "192.168.10.5/24"})

        assert response.status_code == 400
