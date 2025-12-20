"""
Unit tests for netns module.
"""

from unittest.mock import MagicMock, call

import pytest

from arca_storage.cli.lib.netns import attach_vlan, create_namespace, delete_namespace


class TestCreateNamespace:
    """Tests for create_namespace function."""

    @pytest.mark.unit
    def test_create_new_namespace(self, mock_subprocess):
        """Test creating a new namespace."""
        # Mock namespace doesn't exist
        mock_subprocess.side_effect = [
            MagicMock(returncode=0, stdout=""),  # ip netns list (empty)
            MagicMock(returncode=0),  # ip netns add
        ]

        create_namespace("test_ns")

        assert mock_subprocess.call_count == 2
        mock_subprocess.assert_any_call(["ip", "netns", "list"], capture_output=True, text=True, check=False)
        mock_subprocess.assert_any_call(["ip", "netns", "add", "test_ns"], capture_output=True, text=True, check=False)

    @pytest.mark.unit
    def test_namespace_already_exists(self, mock_subprocess):
        """Test creating namespace that already exists."""
        # Mock namespace exists
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="test_ns\n")

        # Should not raise error, just skip
        create_namespace("test_ns")

    @pytest.mark.unit
    def test_create_namespace_fails(self, mock_subprocess):
        """Test creating namespace fails."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0, stdout=""),  # ip netns list (empty)
            MagicMock(returncode=1, stderr="Error"),  # ip netns add fails
        ]

        with pytest.raises(RuntimeError, match="Failed to create namespace"):
            create_namespace("test_ns")


class TestAttachVlan:
    """Tests for attach_vlan function."""

    @pytest.mark.unit
    def test_attach_new_vlan(self, mock_subprocess):
        """Test attaching a new VLAN interface."""
        # Mock VLAN doesn't exist, then create and configure
        mock_subprocess.side_effect = [
            MagicMock(returncode=1),  # ip link show (doesn't exist)
            MagicMock(returncode=0),  # ip link add
            MagicMock(returncode=0),  # ip link set netns
            MagicMock(returncode=0, stdout=""),  # ip addr show (no IP)
            MagicMock(returncode=0),  # ip addr add
            MagicMock(returncode=0),  # ip link set up
        ]

        attach_vlan("test_ns", "bond0", 100, "192.168.10.5/24", None, 1500)

        assert mock_subprocess.call_count >= 6

    @pytest.mark.unit
    def test_attach_existing_vlan(self, mock_subprocess):
        """Test attaching VLAN that already exists."""
        # Mock VLAN exists but not in namespace
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # ip link show (exists)
            MagicMock(returncode=1),  # ip netns exec (not in namespace)
            MagicMock(returncode=0),  # ip link set netns
            MagicMock(returncode=0, stdout=""),  # ip addr show (no IP)
            MagicMock(returncode=0),  # ip addr add
            MagicMock(returncode=0),  # ip link set up
        ]

        attach_vlan("test_ns", "bond0", 100, "192.168.10.5/24", None, 1500)

    @pytest.mark.unit
    def test_attach_vlan_with_gateway(self, mock_subprocess):
        """Test attaching VLAN with gateway."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=1),  # ip link show (doesn't exist)
            MagicMock(returncode=0),  # ip link add
            MagicMock(returncode=0),  # ip link set netns
            MagicMock(returncode=0, stdout=""),  # ip addr show (no IP)
            MagicMock(returncode=0),  # ip addr add
            MagicMock(returncode=0),  # ip link set up
            MagicMock(returncode=0),  # ip route del default
            MagicMock(returncode=0),  # ip route add default
        ]

        attach_vlan("test_ns", "bond0", 100, "192.168.10.5/24", "192.168.10.1", 1500)

        # Check gateway route was added
        mock_subprocess.assert_any_call(
            ["ip", "netns", "exec", "test_ns", "ip", "route", "add", "default", "via", "192.168.10.1"], check=True
        )


class TestDeleteNamespace:
    """Tests for delete_namespace function."""

    @pytest.mark.unit
    def test_delete_existing_namespace(self, mock_subprocess):
        """Test deleting an existing namespace."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0, stdout="test_ns\n"),  # ip netns list
            MagicMock(returncode=0),  # ip netns del
        ]

        delete_namespace("test_ns")

        mock_subprocess.assert_any_call(["ip", "netns", "del", "test_ns"], capture_output=True, text=True, check=False)

    @pytest.mark.unit
    def test_delete_nonexistent_namespace(self, mock_subprocess):
        """Test deleting a namespace that doesn't exist."""
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")  # namespace not in list

        # Should not raise error, just skip
        delete_namespace("test_ns")

    @pytest.mark.unit
    def test_delete_namespace_fails(self, mock_subprocess):
        """Test deleting namespace fails."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0, stdout="test_ns\n"),  # ip netns list
            MagicMock(returncode=1, stderr="Error"),  # ip netns del fails
        ]

        with pytest.raises(RuntimeError, match="Failed to delete namespace"):
            delete_namespace("test_ns")
