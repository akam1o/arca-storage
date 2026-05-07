"""
Scenario tests for end-to-end workflows.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from arca_storage.api.main import app as api_app
from arca_storage.cli.cli import app as cli_app
from arca_storage.errors import NotFoundError


class TestSVMWorkflow:
    """End-to-end scenario: Create SVM -> Create Volume -> Add Export -> Delete."""

    @pytest.mark.integration
    @pytest.mark.slow
    def test_full_svm_lifecycle(self, fake_context):
        """Test complete SVM lifecycle using fake adapters."""
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

        # Verify cleanup
        assert not fake_context.adapters.netns.namespace_exists("tenant_a")
        assert not fake_context.adapters.lvm.lv_exists("vg_pool_01", "vol_tenant_a_vol1")


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
            "status": "Ready",
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
            "status": "Ready",
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
            "status": "Ready",
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
    def test_svm_create_failure_rollback(self, fake_context):
        """Test SVM creation failure when adapter raises."""
        # Break the pacemaker adapter to force a failure at that step
        fake_context.adapters.pacemaker.groups = None

        runner = CliRunner()
        result = runner.invoke(cli_app, ["svm", "create", "tenant_a", "--vlan", "100", "--ip", "192.168.10.5/24"])

        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    @pytest.mark.integration
    def test_failed_svm_create_retries_existing_record(self, fake_context):
        """A repeated create resumes a failed SVM instead of returning AlreadyExists."""
        client = TestClient(api_app, raise_server_exceptions=False)
        fake_context.adapters.pacemaker.groups = None

        response = client.post(
            "/v1/svms",
            json={"name": "tenant_retry", "vlan_id": 100, "ip_cidr": "192.168.10.5/24"},
        )
        assert response.status_code == 500
        assert fake_context.db.get_svm("tenant_retry")["status"]["phase"] == "Failed"

        fake_context.adapters.pacemaker.groups = {}
        response = client.post(
            "/v1/svms",
            json={"name": "tenant_retry", "vlan_id": 100, "ip_cidr": "192.168.10.5/24"},
        )

        assert response.status_code == 201
        assert response.json()["data"]["svm"]["status"] == "Ready"

    @pytest.mark.integration
    @patch("arca_storage.api.services.svm_service.create_svm")
    def test_api_error_response(self, mock_create_svm):
        """Test API returns proper error response."""
        mock_create_svm.side_effect = NotFoundError("SVM", "tenant_a")

        client = TestClient(api_app)
        response = client.post("/v1/svms", json={"name": "tenant_a", "vlan_id": 100, "ip_cidr": "192.168.10.5/24"})

        assert response.status_code == 404
