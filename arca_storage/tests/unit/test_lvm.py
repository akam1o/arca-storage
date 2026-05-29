"""
Unit tests for lvm module.
"""

from unittest.mock import MagicMock

import pytest

from arca_storage.adapters.lvm import SubprocessLVMAdapter
from arca_storage.cli.lib import lvm as legacy_lvm
from arca_storage.cli.lib.lvm import create_lv, create_snapshot_lv, delete_lv, resize_lv
from arca_storage.errors import AlreadyExistsError, PreconditionFailedError


def _assert_redacted(error: BaseException, *values: str) -> None:
    rendered = str(error.to_dict() if hasattr(error, "to_dict") else error)
    for value in values:
        assert value not in rendered


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
            timeout=legacy_lvm._DEFAULT_COMMAND_TIMEOUT_SECONDS,
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
            ["lvcreate", "-L", "100G", "-n", "vol1", "vg_pool_01"],
            capture_output=True,
            text=True,
            check=False,
            timeout=legacy_lvm._DEFAULT_COMMAND_TIMEOUT_SECONDS,
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
            MagicMock(
                returncode=1, stderr="secret-token /dev/vg_pool_01/vol1"
            ),  # lvcreate fails
        ]

        with pytest.raises(
            RuntimeError, match="Failed to create logical volume"
        ) as exc_info:
            create_lv("vg_pool_01", "vol1", 100, thin=True)

        _assert_redacted(exc_info.value, "secret-token", "/dev/vg_pool_01/vol1")


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
            ["lvextend", "-L", "200G", "/dev/vg_pool_01/vol1"],
            capture_output=True,
            text=True,
            check=False,
            timeout=legacy_lvm._DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )

    @pytest.mark.unit
    def test_resize_nonexistent_lv(self, mock_subprocess):
        """Test resizing LV that doesn't exist."""
        mock_subprocess.return_value = MagicMock(
            returncode=1
        )  # lvdisplay (doesn't exist)

        with pytest.raises(RuntimeError, match="does not exist"):
            resize_lv("vg_pool_01", "vol1", 200)

    @pytest.mark.unit
    def test_resize_lv_fails(self, mock_subprocess):
        """Test resizing LV fails."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # lvdisplay (exists)
            MagicMock(returncode=0, stdout="100.00\n"),  # lvs current size
            MagicMock(
                returncode=1, stderr="secret-token /dev/vg_pool_01/vol1"
            ),  # lvextend fails
        ]

        with pytest.raises(
            RuntimeError, match="Failed to resize logical volume"
        ) as exc_info:
            resize_lv("vg_pool_01", "vol1", 200)

        _assert_redacted(exc_info.value, "secret-token", "/dev/vg_pool_01/vol1")

    @pytest.mark.unit
    def test_resize_lv_inspect_size_failure_redacts_stderr(self, mock_subprocess):
        """Test size inspection failure does not expose lvs stderr."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # lvdisplay (exists)
            MagicMock(
                returncode=1, stderr="secret-token /dev/vg_pool_01/vol1"
            ),  # lvs fails
        ]

        with pytest.raises(
            RuntimeError, match="Failed to inspect logical volume size"
        ) as exc_info:
            resize_lv("vg_pool_01", "vol1", 200)

        _assert_redacted(exc_info.value, "secret-token", "/dev/vg_pool_01/vol1")

    @pytest.mark.unit
    def test_resize_lv_empty_size_output_redacts_lv_path(self, mock_subprocess):
        """Test unexpected lvs output does not expose the LV path."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # lvdisplay (exists)
            MagicMock(returncode=0, stdout=""),  # lvs output is empty
        ]

        with pytest.raises(
            RuntimeError, match="Unexpected logical volume size output"
        ) as exc_info:
            resize_lv("vg_pool_01", "vol1", 200)

        _assert_redacted(exc_info.value, "/dev/vg_pool_01/vol1")

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
            ["lvremove", "-f", "/dev/vg_pool_01/vol1"],
            capture_output=True,
            text=True,
            check=False,
            timeout=legacy_lvm._DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )

    @pytest.mark.unit
    def test_delete_nonexistent_lv(self, mock_subprocess):
        """Test deleting LV that doesn't exist."""
        mock_subprocess.return_value = MagicMock(
            returncode=1
        )  # lvdisplay (doesn't exist)

        # Should not raise error, just skip
        delete_lv("vg_pool_01", "vol1")

    @pytest.mark.unit
    def test_delete_lv_fails(self, mock_subprocess):
        """Test deleting LV fails."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # lvdisplay (exists)
            MagicMock(
                returncode=1, stderr="secret-token /dev/vg_pool_01/vol1"
            ),  # lvremove fails
        ]

        with pytest.raises(
            RuntimeError, match="Failed to delete logical volume"
        ) as exc_info:
            delete_lv("vg_pool_01", "vol1")

        _assert_redacted(exc_info.value, "secret-token", "/dev/vg_pool_01/vol1")


class TestCreateSnapshotLv:
    """Tests for create_snapshot_lv function."""

    @pytest.mark.unit
    def test_create_snapshot_lv_failure_redacts_stderr(self, mock_subprocess):
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # source lvdisplay succeeds
            MagicMock(returncode=1),  # snapshot does not exist
            MagicMock(
                returncode=1, stderr="secret-token /dev/vg_pool_01/source"
            ),  # lvcreate fails
        ]

        with pytest.raises(RuntimeError, match="Failed to create snapshot") as exc_info:
            create_snapshot_lv("vg_pool_01", "source", "snap")

        _assert_redacted(exc_info.value, "secret-token", "/dev/vg_pool_01/source")


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

        with pytest.raises(PreconditionFailedError) as exc_info:
            SubprocessLVMAdapter().resize_lv("vg_pool_01", "vol1", 200)

        _assert_redacted(exc_info.value, "/dev/vg_pool_01/vol1", "vg_pool_01", "vol1")
        assert exc_info.value.details == {
            "resource": "LogicalVolume",
            "current_size_gib": 250.0,
            "requested_size_gib": 200,
        }
        calls = [c.args[0] for c in mock_subprocess.call_args_list]
        assert ["lvextend", "-L", "200G", "/dev/vg_pool_01/vol1"] not in calls

    @pytest.mark.unit
    def test_create_thin_lv_already_exists_redacts_lv_path(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(returncode=0)

        with pytest.raises(AlreadyExistsError) as exc_info:
            SubprocessLVMAdapter().create_thin_lv("vg_pool_01", "pool", "vol1", 100)

        _assert_redacted(exc_info.value, "/dev/vg_pool_01/vol1", "vg_pool_01", "vol1")

    @pytest.mark.unit
    def test_get_lv_info_empty_output_redacts_lv_path(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        with pytest.raises(RuntimeError, match="Unexpected lvs output") as exc_info:
            SubprocessLVMAdapter().get_lv_info("vg_pool_01", "vol1")

        _assert_redacted(exc_info.value, "/dev/vg_pool_01/vol1", "vg_pool_01", "vol1")

    @pytest.mark.unit
    def test_get_lv_info_invalid_size_redacts_backend_output(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="secret-output,,,\n"
        )

        with pytest.raises(RuntimeError, match="Unexpected lvs output") as exc_info:
            SubprocessLVMAdapter().get_lv_info("vg_pool_01", "vol1")

        _assert_redacted(exc_info.value, "secret-output", "/dev/vg_pool_01/vol1")

    @pytest.mark.unit
    def test_get_vg_capacity_parses_approximate_values(self, mock_subprocess):
        """Test parsing common vgs output with approximate value markers."""
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="  <931.51,<123.45\n"
        )

        result = SubprocessLVMAdapter().get_vg_capacity("vg_pool_01")

        assert result == {"total_gb": 931.51, "free_gb": 123.45}

    @pytest.mark.unit
    def test_get_vg_capacity_invalid_output_redacts_backend_output(
        self, mock_subprocess
    ):
        """Test malformed vgs output does not expose backend output or VG name."""
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="secret-output\n")

        with pytest.raises(RuntimeError, match="Unexpected vgs output") as exc_info:
            SubprocessLVMAdapter().get_vg_capacity("vg_pool_01")

        _assert_redacted(exc_info.value, "secret-output", "vg_pool_01")
