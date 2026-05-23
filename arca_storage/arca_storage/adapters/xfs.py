"""
XFS filesystem adapter.
"""

from __future__ import annotations

import os
from typing import Optional, Protocol, runtime_checkable

from arca_storage.adapters._subprocess import run_cmd
from arca_storage.errors import NotFoundError, PreconditionFailedError


@runtime_checkable
class XFSAdapter(Protocol):
    def format_xfs(self, device: str) -> None: ...
    def mount(self, device: str, mount_point: str, *, extra_options: Optional[list[str]] = None) -> None: ...
    def umount(self, mount_point: str) -> None: ...
    def grow(self, mount_point: str) -> None: ...
    def is_mounted(self, mount_point: str) -> bool: ...


class SubprocessXFSAdapter:
    """Production adapter — calls real mkfs.xfs / mount commands."""

    def __init__(self, timeout: int = 60) -> None:
        self._timeout = timeout

    def format_xfs(self, device: str) -> None:
        if not os.path.exists(device):
            raise NotFoundError("Device", "<device>")
        # Check if already formatted
        result = run_cmd(["blkid", device], timeout=self._timeout, check=False)
        if result.returncode == 0:
            if 'type="xfs"' in result.stdout.lower():
                return  # idempotent
            raise PreconditionFailedError(
                "Device already contains a non-XFS filesystem",
                {"resource": "Device"},
            )
        run_cmd(
            [
                "mkfs.xfs",
                "-b", "size=4096",
                "-m", "crc=1,finobt=1",
                "-i", "size=512,maxpct=25",
                "-d", "agcount=32,su=256k,sw=1",
                device,
            ],
            timeout=self._timeout,
        )

    def mount(self, device: str, mount_point: str, *, extra_options: Optional[list[str]] = None) -> None:
        self._ensure_safe_mount_point(mount_point)
        os.makedirs(mount_point, exist_ok=True)
        mounted_source = self._mounted_source(mount_point)
        if mounted_source:
            if self._same_device(mounted_source, device):
                return  # idempotent
            raise PreconditionFailedError(
                "Mount point is already mounted from a different source",
                {"resource": "MountPoint"},
            )
        options = ["rw", "noatime", "nodiratime", "logbsize=256k", "inode64"]
        for option in extra_options or []:
            if option not in options:
                options.append(option)
        mount_options = ",".join(options)
        run_cmd(
            ["mount", "-o", mount_options, device, mount_point],
            timeout=self._timeout,
        )

    def umount(self, mount_point: str) -> None:
        if not self.is_mounted(mount_point):
            return  # idempotent
        run_cmd(["umount", mount_point], timeout=self._timeout)

    def grow(self, mount_point: str) -> None:
        if not self.is_mounted(mount_point):
            raise RuntimeError("Mount point is not mounted")
        run_cmd(["xfs_growfs", mount_point], timeout=self._timeout)

    def is_mounted(self, mount_point: str) -> bool:
        result = run_cmd(
            ["mountpoint", "-q", mount_point],
            timeout=self._timeout,
            check=False,
        )
        return result.returncode == 0

    def _mounted_source(self, mount_point: str) -> Optional[str]:
        result = run_cmd(
            ["findmnt", "--mountpoint", mount_point, "--noheadings", "--output", "SOURCE"],
            timeout=self._timeout,
            check=False,
        )
        if result.returncode != 0:
            return None
        source = result.stdout.strip()
        return source or None

    @staticmethod
    def _ensure_safe_mount_point(mount_point: str) -> None:
        try:
            os.lstat(mount_point)
        except FileNotFoundError:
            return
        if os.path.islink(mount_point) or not os.path.isdir(mount_point):
            raise PreconditionFailedError(
                "Mount point must be a real directory",
                {"resource": "MountPoint"},
            )

    @staticmethod
    def _same_device(mounted_source: str, expected_device: str) -> bool:
        try:
            return os.path.samefile(mounted_source, expected_device)
        except OSError:
            return os.path.realpath(mounted_source) == os.path.realpath(expected_device)


class FakeXFSAdapter:
    """In-memory fake for testing."""

    def __init__(self) -> None:
        self.formatted: set[str] = set()
        self.mounts: dict[str, str] = {}  # mount_point -> device
        self.mount_options: dict[str, list[str]] = {}

    def format_xfs(self, device: str) -> None:
        self.formatted.add(device)

    def mount(self, device: str, mount_point: str, *, extra_options: Optional[list[str]] = None) -> None:
        self.mounts[mount_point] = device
        self.mount_options[mount_point] = list(extra_options or [])

    def umount(self, mount_point: str) -> None:
        self.mounts.pop(mount_point, None)
        self.mount_options.pop(mount_point, None)

    def grow(self, mount_point: str) -> None:
        if mount_point not in self.mounts:
            raise RuntimeError("Mount point is not mounted")

    def is_mounted(self, mount_point: str) -> bool:
        return mount_point in self.mounts
