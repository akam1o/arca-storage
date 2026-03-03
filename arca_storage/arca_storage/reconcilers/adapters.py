"""
Adapter bundle — convenience container for passing all adapters to reconcilers.
"""

from __future__ import annotations

from dataclasses import dataclass

from arca_storage.adapters.ganesha import GaneshaAdapter
from arca_storage.adapters.lvm import LVMAdapter
from arca_storage.adapters.netns import NetNSAdapter
from arca_storage.adapters.pacemaker import PacemakerAdapter
from arca_storage.adapters.systemd import SystemdAdapter
from arca_storage.adapters.xfs import XFSAdapter


@dataclass
class Adapters:
    """Container holding one instance of every system adapter."""

    lvm: LVMAdapter
    xfs: XFSAdapter
    netns: NetNSAdapter
    pacemaker: PacemakerAdapter
    ganesha: GaneshaAdapter
    systemd: SystemdAdapter
