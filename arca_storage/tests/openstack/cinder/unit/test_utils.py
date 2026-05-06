"""Unit tests for ARCA Storage Cinder utilities."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch, mock_open

import pytest

from arca_storage.openstack.cinder import utils as arca_utils
from arca_storage.openstack.cinder import exceptions as arca_exceptions


class TestUtilityFunctions(unittest.TestCase):
    """Test utility functions."""

    def _fake_sparse_cp(self, command, **kwargs):
        """Stand in for GNU cp --sparse=always in platform-neutral unit tests."""
        with open(command[-2], "rb") as source:
            data = source.read()
        with open(command[-1], "wb") as dest:
            dest.write(data)
        return Mock(returncode=0)

    def _fake_rename_noreplace(self, source_path, dest_path):
        if os.path.exists(dest_path):
            raise FileExistsError(dest_path)
        os.rename(source_path, dest_path)

    def test_get_mount_point_for_volume(self):
        """Test mount point generation."""
        base_path = "/var/lib/cinder/mnt"
        volume_id = "test-volume-id-123"

        mount_point = arca_utils.get_mount_point_for_volume(base_path, volume_id)

        assert mount_point.startswith(base_path)
        assert len(mount_point) > len(base_path)

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

    def test_create_volume_file_success(self):
        """Test volume file creation."""
        with tempfile.TemporaryDirectory() as mount_point:
            volume_name = "test-volume"
            size_gb = 1

            result = arca_utils.create_volume_file(mount_point, volume_name, size_gb)

            assert result == os.path.join(mount_point, volume_name)
            assert os.path.exists(result)
            assert os.path.getsize(result) == 1024**3

    def test_create_volume_file_already_exists(self):
        """Test volume file creation when file exists."""
        with tempfile.TemporaryDirectory() as mount_point:
            volume_file = os.path.join(mount_point, "test-volume")
            with open(volume_file, "wb"):
                pass

            with pytest.raises(
                arca_exceptions.ArcaStorageException, match="already exists"
            ):
                arca_utils.create_volume_file(mount_point, "test-volume", 10)

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

    @patch("arca_storage.openstack.cinder.utils.os.remove")
    def test_delete_volume_file_ignores_racing_missing_file(self, mock_remove):
        """Test file deletion remains idempotent if another process removed it."""
        mock_remove.side_effect = FileNotFoundError("gone")

        with patch("arca_storage.openstack.cinder.utils.os.path.exists", return_value=True):
            arca_utils.delete_volume_file("/mnt/test", "test-volume")

        mock_remove.assert_called_once()

    @patch("arca_storage.openstack.cinder.utils.os.remove")
    def test_delete_volume_file_raises_on_remove_failure(self, mock_remove):
        """Test delete failures are reported to the driver."""
        mock_remove.side_effect = PermissionError("denied")

        with patch("arca_storage.openstack.cinder.utils.os.path.exists", return_value=True):
            with pytest.raises(arca_exceptions.ArcaStorageException, match="Failed to delete volume file"):
                arca_utils.delete_volume_file("/mnt/test", "test-volume")

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

    def test_copy_sparse_file_hard_link_fallback_copies_without_overwrite(self):
        """Fallback copy installs the destination using exclusive creation."""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "source")
            dest_path = os.path.join(temp_dir, "dest")
            with open(source_path, "wb") as source:
                source.write(b"source-data")

            with patch(
                "arca_storage.openstack.cinder.utils.os.link",
                side_effect=OSError("hard links unavailable"),
            ):
                with patch(
                    "arca_storage.openstack.cinder.utils.subprocess.run",
                    side_effect=self._fake_sparse_cp,
                ):
                    with patch(
                        "arca_storage.openstack.cinder.utils._rename_noreplace",
                        side_effect=self._fake_rename_noreplace,
                    ):
                        arca_utils.copy_sparse_file(source_path, dest_path)

            with open(dest_path, "rb") as dest:
                assert dest.read() == b"source-data"

    def test_copy_sparse_file_fallback_rejects_concurrent_destination(self):
        """Fallback copy does not overwrite a destination created after the first check."""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "source")
            dest_path = os.path.join(temp_dir, "dest")
            with open(source_path, "wb") as source:
                source.write(b"source-data")

            def create_destination_then_fail(temp_path, final_path):
                with open(final_path, "wb") as dest:
                    dest.write(b"concurrent-data")
                raise FileExistsError(final_path)

            with patch(
                "arca_storage.openstack.cinder.utils.os.link",
                side_effect=create_destination_then_fail,
            ):
                with patch(
                    "arca_storage.openstack.cinder.utils.subprocess.run",
                    side_effect=self._fake_sparse_cp,
                ):
                    with patch(
                        "arca_storage.openstack.cinder.utils._rename_noreplace",
                        side_effect=create_destination_then_fail,
                    ):
                        with pytest.raises(
                            arca_exceptions.ArcaStorageException,
                            match="created by another worker",
                        ):
                            arca_utils.copy_sparse_file(source_path, dest_path)

            with open(dest_path, "rb") as dest:
                assert dest.read() == b"concurrent-data"

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
