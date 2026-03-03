"""
System Adapter layer for Arca Storage.

Protocol-based abstractions over system commands (LVM, XFS, network namespaces,
Pacemaker, NFS-Ganesha, systemd). Each adapter has:

- A Protocol defining the interface
- A production implementation using subprocess calls (with timeouts)
- A fake/in-memory implementation for testing

This eliminates raw subprocess calls from business logic, adds uniform
timeouts, and makes the reconciler fully testable without root.
"""

from arca_storage.adapters.lvm import LVMAdapter, SubprocessLVMAdapter, FakeLVMAdapter
from arca_storage.adapters.xfs import XFSAdapter, SubprocessXFSAdapter, FakeXFSAdapter
from arca_storage.adapters.netns import NetNSAdapter, SubprocessNetNSAdapter, FakeNetNSAdapter
from arca_storage.adapters.pacemaker import PacemakerAdapter, SubprocessPacemakerAdapter, FakePacemakerAdapter
from arca_storage.adapters.ganesha import GaneshaAdapter, SubprocessGaneshaAdapter, FakeGaneshaAdapter
from arca_storage.adapters.systemd import SystemdAdapter, SubprocessSystemdAdapter, FakeSystemdAdapter

__all__ = [
    "LVMAdapter",
    "SubprocessLVMAdapter",
    "FakeLVMAdapter",
    "XFSAdapter",
    "SubprocessXFSAdapter",
    "FakeXFSAdapter",
    "NetNSAdapter",
    "SubprocessNetNSAdapter",
    "FakeNetNSAdapter",
    "PacemakerAdapter",
    "SubprocessPacemakerAdapter",
    "FakePacemakerAdapter",
    "GaneshaAdapter",
    "SubprocessGaneshaAdapter",
    "FakeGaneshaAdapter",
    "SystemdAdapter",
    "SubprocessSystemdAdapter",
    "FakeSystemdAdapter",
]
