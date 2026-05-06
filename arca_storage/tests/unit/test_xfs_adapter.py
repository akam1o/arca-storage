"""Unit tests for the production XFS adapter."""

from __future__ import annotations

import subprocess

import pytest

from arca_storage.adapters import xfs
from arca_storage.errors import PreconditionFailedError


def _completed(cmd: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def test_mount_is_idempotent_for_same_device(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[0] == "findmnt":
            return _completed(cmd, stdout="/dev/vg_pool_01/vol1\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(xfs, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(xfs.os, "makedirs", lambda *_args, **_kwargs: None)

    adapter = xfs.SubprocessXFSAdapter()
    adapter.mount("/dev/vg_pool_01/vol1", "/exports/tenant_a/vol1")

    assert calls == [
        ["findmnt", "--mountpoint", "/exports/tenant_a/vol1", "--noheadings", "--output", "SOURCE"]
    ]


def test_mount_rejects_existing_mount_from_different_device(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[0] == "findmnt":
            return _completed(cmd, stdout="/dev/vg_pool_01/other\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(xfs, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(xfs.os, "makedirs", lambda *_args, **_kwargs: None)

    adapter = xfs.SubprocessXFSAdapter()
    with pytest.raises(PreconditionFailedError) as exc_info:
        adapter.mount("/dev/vg_pool_01/vol1", "/exports/tenant_a/vol1")

    assert exc_info.value.details == {
        "mount_point": "/exports/tenant_a/vol1",
        "mounted_source": "/dev/vg_pool_01/other",
        "expected_device": "/dev/vg_pool_01/vol1",
    }
    assert calls == [
        ["findmnt", "--mountpoint", "/exports/tenant_a/vol1", "--noheadings", "--output", "SOURCE"]
    ]


def test_mount_runs_mount_when_mountpoint_is_free(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[0] == "findmnt":
            return _completed(cmd, returncode=1)
        if cmd[0] == "mount":
            return _completed(cmd)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(xfs, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(xfs.os, "makedirs", lambda *_args, **_kwargs: None)

    adapter = xfs.SubprocessXFSAdapter()
    adapter.mount("/dev/vg_pool_01/vol1", "/exports/tenant_a/vol1", extra_options=["nouuid"])

    assert calls == [
        ["findmnt", "--mountpoint", "/exports/tenant_a/vol1", "--noheadings", "--output", "SOURCE"],
        [
            "mount",
            "-o",
            "rw,noatime,nodiratime,logbsize=256k,inode64,nouuid",
            "/dev/vg_pool_01/vol1",
            "/exports/tenant_a/vol1",
        ],
    ]
