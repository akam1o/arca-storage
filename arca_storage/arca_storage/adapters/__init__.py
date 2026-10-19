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

from arca_storage.adapters.ganesha import (
    FakeGaneshaAdapter,
    GaneshaAdapter,
    SubprocessGaneshaAdapter,
)
from arca_storage.adapters.lvm import FakeLVMAdapter, LVMAdapter, SubprocessLVMAdapter
from arca_storage.adapters.netns import (
    FakeNetNSAdapter,
    NetNSAdapter,
    SubprocessNetNSAdapter,
)
from arca_storage.adapters.pacemaker import (
    FakePacemakerAdapter,
    PacemakerAdapter,
    SubprocessPacemakerAdapter,
)
from arca_storage.adapters.systemd import (
    FakeSystemdAdapter,
    SubprocessSystemdAdapter,
    SystemdAdapter,
)
from arca_storage.adapters.xfs import FakeXFSAdapter, SubprocessXFSAdapter, XFSAdapter

__all__ = [
    "FakeGaneshaAdapter",
    "FakeLVMAdapter",
    "FakeNetNSAdapter",
    "FakePacemakerAdapter",
    "FakeSystemdAdapter",
    "FakeXFSAdapter",
    "GaneshaAdapter",
    "LVMAdapter",
    "NetNSAdapter",
    "PacemakerAdapter",
    "SubprocessGaneshaAdapter",
    "SubprocessLVMAdapter",
    "SubprocessNetNSAdapter",
    "SubprocessPacemakerAdapter",
    "SubprocessSystemdAdapter",
    "SubprocessXFSAdapter",
    "SystemdAdapter",
    "XFSAdapter",
]
