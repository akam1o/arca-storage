"""
Unit tests for lvm module.
"""

from unittest.mock import MagicMock

import pytest

from arca_storage.adapters.lvm import SubprocessLVMAdapter
from arca_storage.cli.lib.lvm import create_lv, delete_lv, resize_lv
from arca_storage.errors import PreconditionFailedError


class TestCreateLv:
    """Tests for create_lv function."""

    @pytest.mark.unit
    def test_create_thin_volume(self, mock_subprocess):
        """Test creating a thin provisioned volume."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=1),  # lvdisplay (doesn't exist)
            MagicMock(returncode=0),  # lvcreate
        ]

        result = create_lv("vg_pool_01", "vol1", 100, thin=True)

        assert result == "/dev/vg_pool_01/vol1"
        mock_subprocess.assert_any_call(
            ["lvcreate", "-V", "100G", "-T", "vg_pool_01/pool", "-n", "vol1"],
            capture_output=True,
            text=True,
            check=False,
        )

    @pytest.mark.unit
    def test_create_regular_volume(self, mock_subprocess):
        """Test creating a regular volume."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=1),  # lvdisplay (doesn't exist)
            MagicMock(returncode=0),  # lvcreate
        ]

        result = create_lv("vg_pool_01", "vol1", 100, thin=False)

        assert result == "/dev/vg_pool_01/vol1"
        mock_subprocess.assert_any_call(
            ["lvcreate", "-L", "100G", "-n", "vol1", "vg_pool_01"], capture_output=True, text=True, check=False
        )

    @pytest.mark.unit
    def test_create_existing_lv(self, mock_subprocess):
        """Test creating LV that already exists."""
        mock_subprocess.return_value = MagicMock(returncode=0)  # lvdisplay (exists)

        with pytest.raises(RuntimeError, match="already exists"):
            create_lv("vg_pool_01", "vol1", 100, thin=True)

    @pytest.mark.unit
    def test_create_lv_fails(self, mock_subprocess):
        """Test creating LV fails."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=1),  # lvdisplay (doesn't exist)
            MagicMock(returncode=1, stderr="Error"),  # lvcreate fails
        ]

        with pytest.raises(RuntimeError, match="Failed to create logical volume"):
            create_lv("vg_pool_01", "vol1", 100, thin=True)


class TestResizeLv:
    """Tests for resize_lv function."""

    @pytest.mark.unit
    def test_resize_lv(self, mock_subprocess):
        """Test resizing an LV."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # lvdisplay (exists)
            MagicMock(returncode=0, stdout="100.00\n"),  # lvs current size
            MagicMock(returncode=0),  # lvextend
        ]

        resize_lv("vg_pool_01", "vol1", 200)

        mock_subprocess.assert_any_call(
            ["lvextend", "-L", "200G", "/dev/vg_pool_01/vol1"], capture_output=True, text=True, check=False
        )

    @pytest.mark.unit
    def test_resize_nonexistent_lv(self, mock_subprocess):
        """Test resizing LV that doesn't exist."""
        mock_subprocess.return_value = MagicMock(returncode=1)  # lvdisplay (doesn't exist)

        with pytest.raises(RuntimeError, match="does not exist"):
            resize_lv("vg_pool_01", "vol1", 200)

    @pytest.mark.unit
    def test_resize_lv_fails(self, mock_subprocess):
        """Test resizing LV fails."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # lvdisplay (exists)
            MagicMock(returncode=0, stdout="100.00\n"),  # lvs current size
            MagicMock(returncode=1, stderr="Error"),  # lvextend fails
        ]

        with pytest.raises(RuntimeError, match="Failed to resize logical volume"):
            resize_lv("vg_pool_01", "vol1", 200)

    @pytest.mark.unit
    def test_resize_lv_skips_already_requested_size(self, mock_subprocess):
        """Test resize retries continue when the LV has already reached target size."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # lvdisplay (exists)
            MagicMock(returncode=0, stdout="200.00\n"),  # lvs current size
        ]

        resize_lv("vg_pool_01", "vol1", 200)

        calls = [c.args[0] for c in mock_subprocess.call_args_list]
        assert ["lvextend", "-L", "200G", "/dev/vg_pool_01/vol1"] not in calls

    @pytest.mark.unit
    def test_resize_lv_rejects_larger_backend_size(self, mock_subprocess):
        """Test a smaller retry does not silently accept a larger LV."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # lvdisplay (exists)
            MagicMock(returncode=0, stdout="250.00\n"),  # lvs current size
        ]

        with pytest.raises(RuntimeError, match="already larger than requested size"):
            resize_lv("vg_pool_01", "vol1", 200)

        calls = [c.args[0] for c in mock_subprocess.call_args_list]
        assert ["lvextend", "-L", "200G", "/dev/vg_pool_01/vol1"] not in calls


class TestDeleteLv:
    """Tests for delete_lv function."""

    @pytest.mark.unit
    def test_delete_lv(self, mock_subprocess):
        """Test deleting an LV."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # lvdisplay (exists)
            MagicMock(returncode=0),  # lvremove
        ]

        delete_lv("vg_pool_01", "vol1")

        mock_subprocess.assert_any_call(
            ["lvremove", "-f", "/dev/vg_pool_01/vol1"], capture_output=True, text=True, check=False
        )

    @pytest.mark.unit
    def test_delete_nonexistent_lv(self, mock_subprocess):
        """Test deleting LV that doesn't exist."""
        mock_subprocess.return_value = MagicMock(returncode=1)  # lvdisplay (doesn't exist)

        # Should not raise error, just skip
        delete_lv("vg_pool_01", "vol1")

    @pytest.mark.unit
    def test_delete_lv_fails(self, mock_subprocess):
        """Test deleting LV fails."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # lvdisplay (exists)
            MagicMock(returncode=1, stderr="Error"),  # lvremove fails
        ]

        with pytest.raises(RuntimeError, match="Failed to delete logical volume"):
            delete_lv("vg_pool_01", "vol1")


class TestSubprocessLVMAdapter:
    @pytest.mark.unit
    def test_resize_lv_skips_already_requested_size(self, mock_subprocess):
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # lvdisplay (exists)
            MagicMock(returncode=0, stdout="200.00\n"),  # lvs current size
        ]

        SubprocessLVMAdapter().resize_lv("vg_pool_01", "vol1", 200)

        calls = [c.args[0] for c in mock_subprocess.call_args_list]
        assert ["lvextend", "-L", "200G", "/dev/vg_pool_01/vol1"] not in calls

    @pytest.mark.unit
    def test_resize_lv_rejects_larger_backend_size(self, mock_subprocess):
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # lvdisplay (exists)
            MagicMock(returncode=0, stdout="250.00\n"),  # lvs current size
        ]

        with pytest.raises(PreconditionFailedError):
            SubprocessLVMAdapter().resize_lv("vg_pool_01", "vol1", 200)

        calls = [c.args[0] for c in mock_subprocess.call_args_list]
        assert ["lvextend", "-L", "200G", "/dev/vg_pool_01/vol1"] not in calls

    @pytest.mark.unit
    def test_get_vg_capacity_parses_approximate_values(self, mock_subprocess):
        """Test parsing common vgs output with approximate value markers."""
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="  <931.51,<123.45\n")

        result = SubprocessLVMAdapter().get_vg_capacity("vg_pool_01")

        assert result == {"total_gb": 931.51, "free_gb": 123.45}
