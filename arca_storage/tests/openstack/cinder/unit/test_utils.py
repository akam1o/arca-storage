"""Unit tests for ARCA Storage Cinder utilities."""

import os
import unittest
from unittest.mock import MagicMock, Mock, patch, mock_open

import pytest

from arca_storage.openstack.cinder import utils as arca_utils
from arca_storage.openstack.cinder import exceptions as arca_exceptions


class TestUtilityFunctions(unittest.TestCase):
    """Test utility functions."""

    def test_get_mount_point_for_volume(self):
        """Test mount point generation."""
        base_path = "/var/lib/cinder/mnt"
        volume_id = "test-volume-id-123"

        mount_point = arca_utils.get_mount_point_for_volume(base_path, volume_id)

        assert mount_point.startswith(base_path)
        assert len(mount_point) > len(base_path)

    def test_get_export_path(self):
        """Test NFS export path generation."""
        svm_vip = "192.168.100.5"
        svm_name = "test-svm"
        volume_name = "test-volume"

        export_path = arca_utils.get_export_path(svm_vip, svm_name, volume_name)

        assert export_path == "192.168.100.5:/exports/test-svm/test-volume"

    @patch("arca_storage.openstack.cinder.utils.os.makedirs")
    def test_ensure_mount_point_exists_success(self, mock_makedirs):
        """Test mount point directory creation."""
        mount_point = "/var/lib/cinder/mnt/test"

        arca_utils.ensure_mount_point_exists(mount_point)

        mock_makedirs.assert_called_once_with(mount_point, mode=0o750, exist_ok=True)

    @patch("arca_storage.openstack.cinder.utils.os.makedirs")
    def test_ensure_mount_point_exists_failure(self, mock_makedirs):
        """Test mount point creation failure."""
        mock_makedirs.side_effect = OSError("Permission denied")

        with pytest.raises(arca_exceptions.ArcaStorageException, match="Failed to create mount point"):
            arca_utils.ensure_mount_point_exists("/test")

    @patch("arca_storage.openstack.cinder.utils.subprocess.run")
    @patch("arca_storage.openstack.cinder.utils.get_nfs_share_info")
    @patch("arca_storage.openstack.cinder.utils.ensure_mount_point_exists")
    def test_mount_nfs_success(self, mock_ensure, mock_share_info, mock_run):
        """Test successful NFS mount."""
        mock_share_info.return_value = None  # Not already mounted

        arca_utils.mount_nfs(
            export_path="192.168.100.5:/exports/svm1/vol1",
            mount_point="/mnt/test",
            mount_options="rw,noatime,vers=4.1",
        )

        mock_ensure.assert_called_once()
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "mount" in args
        assert "-t" in args
        assert "nfs4" in args

    @patch("arca_storage.openstack.cinder.utils.subprocess.run")
    @patch("arca_storage.openstack.cinder.utils.get_nfs_share_info")
    @patch("arca_storage.openstack.cinder.utils.ensure_mount_point_exists")
    def test_mount_nfs_already_mounted(self, mock_ensure, mock_share_info, mock_run):
        """Test mount when already mounted with same export."""
        mock_share_info.return_value = {
            "device": "192.168.100.5:/exports/svm1/vol1",
            "mount_point": "/mnt/test",
        }

        arca_utils.mount_nfs(
            export_path="192.168.100.5:/exports/svm1/vol1",
            mount_point="/mnt/test",
            mount_options="rw,noatime,vers=4.1",
        )

        mock_run.assert_not_called()

    @patch("arca_storage.openstack.cinder.utils.subprocess.run")
    @patch("arca_storage.openstack.cinder.utils.get_nfs_share_info")
    @patch("arca_storage.openstack.cinder.utils.ensure_mount_point_exists")
    def test_mount_nfs_different_export(self, mock_ensure, mock_share_info, mock_run):
        """Test mount when already mounted with different export."""
        mock_share_info.return_value = {
            "device": "192.168.100.5:/exports/svm1/vol2",
            "mount_point": "/mnt/test",
        }

        with pytest.raises(
            arca_exceptions.ArcaStorageException, match="already has different export"
        ):
            arca_utils.mount_nfs(
                export_path="192.168.100.5:/exports/svm1/vol1",
                mount_point="/mnt/test",
                mount_options="rw,noatime,vers=4.1",
            )

    @patch("arca_storage.openstack.cinder.utils.subprocess.run")
    @patch("arca_storage.openstack.cinder.utils.is_mounted")
    def test_unmount_nfs_success(self, mock_is_mounted, mock_run):
        """Test successful NFS unmount."""
        mock_is_mounted.return_value = True

        arca_utils.unmount_nfs("/mnt/test")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "umount" in args

    @patch("arca_storage.openstack.cinder.utils.is_mounted")
    def test_unmount_nfs_not_mounted(self, mock_is_mounted):
        """Test unmount when not mounted."""
        mock_is_mounted.return_value = False

        arca_utils.unmount_nfs("/mnt/test")

        # Should return without error

    @patch("arca_storage.openstack.cinder.utils.lazy_unmount")
    @patch("arca_storage.openstack.cinder.utils.subprocess.run")
    @patch("arca_storage.openstack.cinder.utils.is_mounted")
    def test_unmount_nfs_force_on_failure(self, mock_is_mounted, mock_run, mock_lazy):
        """Test force unmount on failure."""
        import subprocess

        mock_is_mounted.return_value = True
        # Use CalledProcessError which is what subprocess.run raises
        mock_run.side_effect = subprocess.CalledProcessError(1, "umount", stderr="busy")

        arca_utils.unmount_nfs("/mnt/test", force=True)

        mock_lazy.assert_called_once_with("/mnt/test")

    @patch("arca_storage.openstack.cinder.utils.subprocess.run")
    def test_is_mounted_via_proc(self, mock_run):
        """Test mount check via /proc/mounts."""
        mock_data = "/dev/sda1 /mnt/test ext4 rw 0 0\n"

        with patch("builtins.open", mock_open(read_data=mock_data)):
            result = arca_utils.is_mounted("/mnt/test")

        assert result is True

    @patch("arca_storage.openstack.cinder.utils.subprocess.run")
    def test_is_mounted_false(self, mock_run):
        """Test mount check when not mounted."""
        mock_data = "/dev/sda1 /mnt/other ext4 rw 0 0\n"

        with patch("builtins.open", mock_open(read_data=mock_data)):
            result = arca_utils.is_mounted("/mnt/test")

        assert result is False

    def test_get_nfs_share_info_success(self):
        """Test NFS share info retrieval."""
        mock_data = "192.168.100.5:/exports/svm1/vol1 /mnt/test nfs4 rw,vers=4.1 0 0\n"

        with patch("builtins.open", mock_open(read_data=mock_data)):
            result = arca_utils.get_nfs_share_info("/mnt/test")

        assert result is not None
        assert result["device"] == "192.168.100.5:/exports/svm1/vol1"
        assert result["mount_point"] == "/mnt/test"
        assert result["fs_type"] == "nfs4"

    def test_get_nfs_share_info_not_found(self):
        """Test NFS share info when not mounted."""
        mock_data = "192.168.100.5:/exports/svm1/vol1 /mnt/other nfs4 rw 0 0\n"

        with patch("builtins.open", mock_open(read_data=mock_data)):
            result = arca_utils.get_nfs_share_info("/mnt/test")

        assert result is None

    @patch("arca_storage.openstack.cinder.utils.subprocess.run")
    @patch("arca_storage.openstack.cinder.utils.os.chmod")
    def test_create_volume_file_success(self, mock_chmod, mock_run):
        """Test volume file creation."""
        mount_point = "/mnt/test"
        volume_name = "test-volume"
        size_gb = 10

        with patch("arca_storage.openstack.cinder.utils.os.path.exists", return_value=False):
            result = arca_utils.create_volume_file(mount_point, volume_name, size_gb)

        assert result == os.path.join(mount_point, volume_name)
        mock_run.assert_called_once()
        mock_chmod.assert_called_once_with(result, 0o600)

    def test_create_volume_file_already_exists(self):
        """Test volume file creation when file exists."""
        with patch("arca_storage.openstack.cinder.utils.os.path.exists", return_value=True):
            with pytest.raises(
                arca_exceptions.ArcaStorageException, match="already exists"
            ):
                arca_utils.create_volume_file("/mnt/test", "test-volume", 10)

    @patch("arca_storage.openstack.cinder.utils.os.remove")
    def test_delete_volume_file_success(self, mock_remove):
        """Test volume file deletion."""
        with patch("arca_storage.openstack.cinder.utils.os.path.exists", return_value=True):
            arca_utils.delete_volume_file("/mnt/test", "test-volume")

        mock_remove.assert_called_once()

    @patch("arca_storage.openstack.cinder.utils.os.remove")
    def test_delete_volume_file_not_exists(self, mock_remove):
        """Test volume file deletion when file doesn't exist."""
        with patch("arca_storage.openstack.cinder.utils.os.path.exists", return_value=False):
            arca_utils.delete_volume_file("/mnt/test", "test-volume")

        mock_remove.assert_not_called()

    @patch("arca_storage.openstack.cinder.utils.subprocess.run")
    def test_extend_volume_file_success(self, mock_run):
        """Test volume file extension."""
        with patch("arca_storage.openstack.cinder.utils.os.path.exists", return_value=True):
            arca_utils.extend_volume_file("/mnt/test", "test-volume", 20)

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "truncate" in args

    def test_extend_volume_file_not_exists(self):
        """Test volume file extension when file doesn't exist."""
        with patch("arca_storage.openstack.cinder.utils.os.path.exists", return_value=False):
            with pytest.raises(
                arca_exceptions.ArcaStorageException, match="does not exist"
            ):
                arca_utils.extend_volume_file("/mnt/test", "test-volume", 20)

    @patch("arca_storage.openstack.cinder.utils.os.rmdir")
    def test_cleanup_mount_point_success(self, mock_rmdir):
        """Test mount point cleanup."""
        with patch("arca_storage.openstack.cinder.utils.os.path.exists", return_value=True):
            with patch("arca_storage.openstack.cinder.utils.os.path.isdir", return_value=True):
                with patch("arca_storage.openstack.cinder.utils.os.listdir", return_value=[]):
                    arca_utils.cleanup_mount_point("/mnt/test")

        mock_rmdir.assert_called_once()

    @patch("arca_storage.openstack.cinder.utils.os.rmdir")
    def test_cleanup_mount_point_not_empty(self, mock_rmdir):
        """Test mount point cleanup when directory not empty."""
        with patch("arca_storage.openstack.cinder.utils.os.path.exists", return_value=True):
            with patch("arca_storage.openstack.cinder.utils.os.path.isdir", return_value=True):
                with patch("arca_storage.openstack.cinder.utils.os.listdir", return_value=["file"]):
                    arca_utils.cleanup_mount_point("/mnt/test")

        mock_rmdir.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
