"""
Unit tests for xfs module.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from arca_storage.cli.lib.xfs import format_xfs, grow_xfs, mount_xfs, umount_xfs


class TestFormatXfs:
    """Tests for format_xfs function."""

    @pytest.mark.unit
    def test_format_new_device(self, mock_subprocess, mock_path_exists):
        """Test formatting a new device."""
        mock_path_exists.return_value = True
        mock_subprocess.side_effect = [
            MagicMock(returncode=1),  # blkid (not formatted)
            MagicMock(returncode=0),  # mkfs.xfs
        ]

        format_xfs("/dev/vg_pool_01/vol1")

        mock_subprocess.assert_any_call(
            [
                "mkfs.xfs",
                "-b",
                "size=4096",
                "-m",
                "crc=1,finobt=1",
                "-i",
                "size=512,maxpct=25",
                "-d",
                "agcount=32,su=256k,sw=1",
                "/dev/vg_pool_01/vol1",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    @pytest.mark.unit
    def test_format_already_formatted(self, mock_subprocess, mock_path_exists):
        """Test formatting device that's already formatted."""
        mock_path_exists.return_value = True
        mock_subprocess.return_value = MagicMock(returncode=0, stdout='TYPE="xfs"')

        # Should not raise error, just skip
        format_xfs("/dev/vg_pool_01/vol1")

    @pytest.mark.unit
    def test_format_nonexistent_device(self, mock_path_exists):
        """Test formatting device that doesn't exist."""
        mock_path_exists.return_value = False

        with pytest.raises(RuntimeError, match="does not exist"):
            format_xfs("/dev/vg_pool_01/vol1")

    @pytest.mark.unit
    def test_format_fails(self, mock_subprocess, mock_path_exists):
        """Test formatting fails."""
        mock_path_exists.return_value = True
        mock_subprocess.side_effect = [
            MagicMock(returncode=1),  # blkid (not formatted)
            MagicMock(returncode=1, stderr="Error"),  # mkfs.xfs fails
        ]

        with pytest.raises(RuntimeError, match="Failed to format XFS"):
            format_xfs("/dev/vg_pool_01/vol1")


class TestMountXfs:
    """Tests for mount_xfs function."""

    @pytest.mark.unit
    @patch("os.makedirs")
    def test_mount_new_filesystem(self, mock_makedirs, mock_subprocess):
        """Test mounting a new filesystem."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=1),  # mountpoint (not mounted)
            MagicMock(returncode=0),  # mount
        ]

        mount_xfs("/dev/vg_pool_01/vol1", "/exports/tenant_a/vol1")

        mock_makedirs.assert_called_once_with("/exports/tenant_a/vol1", exist_ok=True)
        mock_subprocess.assert_any_call(
            [
                "mount",
                "-o",
                "rw,noatime,nodiratime,logbsize=256k,inode64",
                "/dev/vg_pool_01/vol1",
                "/exports/tenant_a/vol1",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    @pytest.mark.unit
    @patch("os.makedirs")
    def test_mount_already_mounted(self, mock_makedirs, mock_subprocess):
        """Test mounting filesystem that's already mounted."""
        mock_subprocess.return_value = MagicMock(returncode=0)  # mountpoint (mounted)

        # Should not raise error, just skip
        mount_xfs("/dev/vg_pool_01/vol1", "/exports/tenant_a/vol1")

    @pytest.mark.unit
    @patch("os.makedirs")
    def test_mount_fails(self, mock_makedirs, mock_subprocess):
        """Test mounting fails."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=1),  # mountpoint (not mounted)
            MagicMock(returncode=1, stderr="Error"),  # mount fails
        ]

        with pytest.raises(RuntimeError, match="Failed to mount XFS"):
            mount_xfs("/dev/vg_pool_01/vol1", "/exports/tenant_a/vol1")


class TestUmountXfs:
    """Tests for umount_xfs function."""

    @pytest.mark.unit
    def test_umount_mounted_filesystem(self, mock_subprocess):
        """Test unmounting a mounted filesystem."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # mountpoint (mounted)
            MagicMock(returncode=0),  # umount
        ]

        umount_xfs("/exports/tenant_a/vol1")

        mock_subprocess.assert_any_call(
            ["umount", "/exports/tenant_a/vol1"], capture_output=True, text=True, check=False
        )

    @pytest.mark.unit
    def test_umount_not_mounted(self, mock_subprocess):
        """Test unmounting filesystem that's not mounted."""
        mock_subprocess.return_value = MagicMock(returncode=1)  # mountpoint (not mounted)

        # Should not raise error, just skip
        umount_xfs("/exports/tenant_a/vol1")

    @pytest.mark.unit
    def test_umount_fails(self, mock_subprocess):
        """Test unmounting fails."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # mountpoint (mounted)
            MagicMock(returncode=1, stderr="Error"),  # umount fails
        ]

        with pytest.raises(RuntimeError, match="Failed to unmount XFS"):
            umount_xfs("/exports/tenant_a/vol1")


class TestGrowXfs:
    """Tests for grow_xfs function."""

    @pytest.mark.unit
    def test_grow_xfs(self, mock_subprocess):
        """Test growing XFS filesystem."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # mountpoint (mounted)
            MagicMock(returncode=0),  # xfs_growfs
        ]

        grow_xfs("/exports/tenant_a/vol1")

        mock_subprocess.assert_any_call(
            ["xfs_growfs", "/exports/tenant_a/vol1"], capture_output=True, text=True, check=False
        )

    @pytest.mark.unit
    def test_grow_not_mounted(self, mock_subprocess):
        """Test growing filesystem that's not mounted."""
        mock_subprocess.return_value = MagicMock(returncode=1)  # mountpoint (not mounted)

        with pytest.raises(RuntimeError, match="is not mounted"):
            grow_xfs("/exports/tenant_a/vol1")

    @pytest.mark.unit
    def test_grow_fails(self, mock_subprocess):
        """Test growing filesystem fails."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # mountpoint (mounted)
            MagicMock(returncode=1, stderr="Error"),  # xfs_growfs fails
        ]

        with pytest.raises(RuntimeError, match="Failed to grow XFS"):
            grow_xfs("/exports/tenant_a/vol1")
