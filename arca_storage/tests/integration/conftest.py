"""
Shared fixtures for integration tests.

Provides a fake AppContext with in-memory adapters and SQLite DB,
replacing the old approach of patching individual module-level functions.
"""

from types import SimpleNamespace

import pytest

from arca_storage.adapters.ganesha import FakeGaneshaAdapter
from arca_storage.adapters.lvm import FakeLVMAdapter
from arca_storage.adapters.netns import FakeNetNSAdapter
from arca_storage.adapters.pacemaker import FakePacemakerAdapter
from arca_storage.adapters.systemd import FakeSystemdAdapter
from arca_storage.adapters.xfs import FakeXFSAdapter
from arca_storage.db import StateDB
from arca_storage.reconcilers.adapters import Adapters
from arca_storage.reconcilers.export import ExportReconciler
from arca_storage.reconcilers.snapshot import SnapshotReconciler
from arca_storage.reconcilers.svm import SVMReconciler
from arca_storage.reconcilers.volume import VolumeReconciler


class FakeSettings:
    """Minimal settings that provides to_reconciler_config()."""

    def __init__(self):
        self.csi = SimpleNamespace(client_cidrs=["10.0.0.0/24"], root_squash=True)

    def to_reconciler_config(self):
        return {
            "vg_name": "vg_pool_01",
            "thinpool_name": "pool",
            "export_dir": "/exports",
            "parent_if": "bond0",
            "drbd_resource": "r0",
            "ganesha_config_dir": "/tmp/ganesha",
        }


class FakeAppContext:
    """AppContext with in-memory fakes — no subprocess calls, no config file."""

    def __init__(self, db_path: str):
        self.db = StateDB(db_path)
        self.settings = FakeSettings()
        self.adapters = Adapters(
            lvm=FakeLVMAdapter(),
            xfs=FakeXFSAdapter(),
            netns=FakeNetNSAdapter(),
            pacemaker=FakePacemakerAdapter(),
            ganesha=FakeGaneshaAdapter(),
            systemd=FakeSystemdAdapter(),
        )
        cfg = self.settings.to_reconciler_config()
        self.svm_reconciler = SVMReconciler(self.db, self.adapters, config=cfg)
        self.volume_reconciler = VolumeReconciler(self.db, self.adapters, config=cfg)
        self.snapshot_reconciler = SnapshotReconciler(self.db, self.adapters, config=cfg)
        self.export_reconciler = ExportReconciler(self.db, self.adapters, config=cfg)

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def fake_context(tmp_path):
    """Provide a FakeAppContext and set it as the global context."""
    ctx = FakeAppContext(str(tmp_path / "test.db"))
    # Inject into the module-level variable so get_context() returns it
    import arca_storage.context as context_mod
    context_mod.reset_context(ctx)
    yield ctx
    context_mod.reset_context(None)
