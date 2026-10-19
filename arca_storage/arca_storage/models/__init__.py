"""
Declarative resource models for Arca Storage.

Uses Spec/Status pattern inspired by Kubernetes:
- Spec: User-declared desired state (immutable after creation)
- Status: System-managed actual state (updated by reconcilers)
"""

from arca_storage.models.base import Phase, ResourceMeta
from arca_storage.models.export import Export, ExportSpec, ExportStatus
from arca_storage.models.snapshot import Snapshot, SnapshotSpec, SnapshotStatus
from arca_storage.models.svm import SVM, SVMSpec, SVMStatus
from arca_storage.models.volume import QoSStatus, Volume, VolumeSpec, VolumeStatus

__all__ = [
    "SVM",
    "Export",
    "ExportSpec",
    "ExportStatus",
    "Phase",
    "QoSStatus",
    "ResourceMeta",
    "SVMSpec",
    "SVMStatus",
    "Snapshot",
    "SnapshotSpec",
    "SnapshotStatus",
    "Volume",
    "VolumeSpec",
    "VolumeStatus",
]
