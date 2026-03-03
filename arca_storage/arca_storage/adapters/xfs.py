"""
XFS filesystem adapter.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from arca_storage.adapters._subprocess import run_cmd
from arca_storage.errors import NotFoundError


@runtime_checkable
class XFSAdapter(Protocol):
    def format_xfs(self, device: str) -> None: ...
    def mount(self, device: str, mount_point: str) -> None: ...
    def umount(self, mount_point: str) -> None: ...
    def grow(self, mount_point: str) -> None: ...
    def is_mounted(self, mount_point: str) -> bool: ...


class SubprocessXFSAdapter:
    """Production adapter — calls real mkfs.xfs / mount commands."""

    def __init__(self, timeout: int = 60) -> None:
        self._timeout = timeout

    def format_xfs(self, device: str) -> None:
        if not os.path.exists(device):
            raise NotFoundError("Device", device)
        # Check if already formatted
        result = run_cmd(["blkid", device], timeout=self._timeout, check=False)
        if result.returncode == 0 and "xfs" in result.stdout.lower():
            return  # idempotent
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

    def mount(self, device: str, mount_point: str) -> None:
        os.makedirs(mount_point, exist_ok=True)
        if self.is_mounted(mount_point):
            return  # idempotent
        mount_options = "rw,noatime,nodiratime,logbsize=256k,inode64"
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
            raise RuntimeError(f"Mount point {mount_point} is not mounted")
        run_cmd(["xfs_growfs", mount_point], timeout=self._timeout)

    def is_mounted(self, mount_point: str) -> bool:
        result = run_cmd(
            ["mountpoint", "-q", mount_point],
            timeout=self._timeout,
            check=False,
        )
        return result.returncode == 0


class FakeXFSAdapter:
    """In-memory fake for testing."""

    def __init__(self) -> None:
        self.formatted: set[str] = set()
        self.mounts: dict[str, str] = {}  # mount_point -> device

    def format_xfs(self, device: str) -> None:
        self.formatted.add(device)

    def mount(self, device: str, mount_point: str) -> None:
        self.mounts[mount_point] = device

    def umount(self, mount_point: str) -> None:
        self.mounts.pop(mount_point, None)

    def grow(self, mount_point: str) -> None:
        if mount_point not in self.mounts:
            raise RuntimeError(f"Mount point {mount_point} is not mounted")

    def is_mounted(self, mount_point: str) -> bool:
        return mount_point in self.mounts
