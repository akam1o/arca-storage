"""
Unit tests for systemd CLI helpers.
"""

from unittest.mock import MagicMock

import pytest

from arca_storage.cli.lib import systemd as legacy_systemd
from arca_storage.cli.lib.systemd import is_active, start_unit, stop_unit


def _assert_redacted(error: BaseException, *values: str) -> None:
    rendered = str(error)
    for value in values:
        assert value not in rendered


class TestSystemdUnits:
    @pytest.mark.unit
    def test_start_unit_success(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(returncode=0)

        start_unit("nfs-ganesha@tenant_a")

        mock_subprocess.assert_called_once_with(
            ["systemctl", "start", "nfs-ganesha@tenant_a"],
            capture_output=True,
            text=True,
            check=False,
            timeout=legacy_systemd._DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )

    @pytest.mark.unit
    def test_start_unit_failure_redacts_unit_and_stderr(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(
            returncode=1, stderr="secret-token nfs-ganesha@tenant_a"
        )

        with pytest.raises(
            RuntimeError, match="Failed to start systemd unit"
        ) as exc_info:
            start_unit("nfs-ganesha@tenant_a")

        _assert_redacted(exc_info.value, "secret-token", "nfs-ganesha@tenant_a")

    @pytest.mark.unit
    def test_stop_unit_success(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(returncode=0)

        stop_unit("nfs-ganesha@tenant_a")

        mock_subprocess.assert_called_once_with(
            ["systemctl", "stop", "nfs-ganesha@tenant_a"],
            capture_output=True,
            text=True,
            check=False,
            timeout=legacy_systemd._DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )

    @pytest.mark.unit
    def test_stop_unit_failure_redacts_unit_and_stderr(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(
            returncode=1, stderr="secret-token nfs-ganesha@tenant_a"
        )

        with pytest.raises(
            RuntimeError, match="Failed to stop systemd unit"
        ) as exc_info:
            stop_unit("nfs-ganesha@tenant_a")

        _assert_redacted(exc_info.value, "secret-token", "nfs-ganesha@tenant_a")

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("returncode", "expected"),
        [
            (0, True),
            (3, False),
        ],
    )
    def test_is_active(self, mock_subprocess, returncode, expected):
        mock_subprocess.return_value = MagicMock(returncode=returncode)

        assert is_active("nfs-ganesha@tenant_a") is expected

        mock_subprocess.assert_called_once_with(
            ["systemctl", "is-active", "nfs-ganesha@tenant_a"],
            capture_output=True,
            text=True,
            check=False,
            timeout=legacy_systemd._DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
