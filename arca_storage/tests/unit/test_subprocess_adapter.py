"""Tests for the shared subprocess adapter."""

import subprocess

import pytest

from arca_storage.adapters._subprocess import run_cmd
from arca_storage.errors import TimeoutError as ArcaTimeoutError


def test_run_cmd_timeout_redacts_command_arguments(monkeypatch):
    def timeout_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            [
                "/usr/sbin/lvcreate",
                "/dev/vg_arca/secret-volume",
                "--name",
                "secret-token",
            ],
            timeout=30,
        )

    monkeypatch.setattr(subprocess, "run", timeout_run)

    with pytest.raises(ArcaTimeoutError) as exc_info:
        run_cmd(
            [
                "/usr/sbin/lvcreate",
                "/dev/vg_arca/secret-volume",
                "--name",
                "secret-token",
            ],
            timeout=30,
        )

    error = exc_info.value.to_dict()
    assert error["details"] == {
        "operation": "lvcreate",
        "timeout_seconds": 30,
    }
    rendered = str(error)
    assert "/dev/vg_arca/secret-volume" not in rendered
    assert "secret-token" not in rendered
