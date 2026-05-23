"""Unit tests for the production XFS adapter."""

from __future__ import annotations

import subprocess

import pytest

from arca_storage.adapters import xfs
from arca_storage.errors import PreconditionFailedError


def _completed(cmd: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def _assert_redacted(error, *values: str) -> None:
    rendered = str(error.to_dict() if hasattr(error, "to_dict") else error)
    for value in values:
        assert value not in rendered


def test_format_skips_existing_xfs_filesystem(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[0] == "blkid":
            return _completed(cmd, stdout='/dev/vg_pool_01/vol1: UUID="abc" TYPE="xfs"\n')
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(xfs.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(xfs, "run_cmd", fake_run_cmd)

    adapter = xfs.SubprocessXFSAdapter()
    adapter.format_xfs("/dev/vg_pool_01/vol1")

    assert calls == [["blkid", "/dev/vg_pool_01/vol1"]]


def test_format_rejects_existing_non_xfs_filesystem(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[0] == "blkid":
            return _completed(cmd, stdout='/dev/vg_pool_01/vol1: UUID="abc" TYPE="ext4"\n')
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(xfs.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(xfs, "run_cmd", fake_run_cmd)

    adapter = xfs.SubprocessXFSAdapter()
    with pytest.raises(PreconditionFailedError) as exc_info:
        adapter.format_xfs("/dev/vg_pool_01/vol1")

    assert exc_info.value.details == {"resource": "Device"}
    _assert_redacted(exc_info.value, "/dev/vg_pool_01/vol1", "UUID", "ext4")
    assert calls == [["blkid", "/dev/vg_pool_01/vol1"]]


def test_format_formats_device_without_existing_signature(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[0] == "blkid":
            return _completed(cmd, returncode=2)
        if cmd[0] == "mkfs.xfs":
            return _completed(cmd)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(xfs.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(xfs, "run_cmd", fake_run_cmd)

    adapter = xfs.SubprocessXFSAdapter()
    adapter.format_xfs("/dev/vg_pool_01/vol1")

    assert calls == [
        ["blkid", "/dev/vg_pool_01/vol1"],
        [
            "mkfs.xfs",
            "-b", "size=4096",
            "-m", "crc=1,finobt=1",
            "-i", "size=512,maxpct=25",
            "-d", "agcount=32,su=256k,sw=1",
            "/dev/vg_pool_01/vol1",
        ],
    ]


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

    assert exc_info.value.details == {"resource": "MountPoint"}
    _assert_redacted(exc_info.value, "/exports/tenant_a/vol1", "/dev/vg_pool_01/other", "/dev/vg_pool_01/vol1")
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


def test_grow_unmounted_redacts_mount_path(monkeypatch):
    def fake_run_cmd(cmd, **_kwargs):
        if cmd[0] == "mountpoint":
            return _completed(cmd, returncode=1)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(xfs, "run_cmd", fake_run_cmd)

    adapter = xfs.SubprocessXFSAdapter()
    with pytest.raises(RuntimeError, match="Mount point is not mounted") as exc_info:
        adapter.grow("/exports/tenant_a/vol1")

    _assert_redacted(exc_info.value, "/exports/tenant_a/vol1")
