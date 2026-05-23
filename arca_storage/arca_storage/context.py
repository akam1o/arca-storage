"""
Application context — wires together config, DB, adapters, and reconcilers.

Used as the single entry-point for both the API server and the CLI to
obtain fully-configured reconcilers without constructing dependencies
manually everywhere.
"""

from __future__ import annotations

from typing import Optional

from arca_storage.adapters.ganesha import SubprocessGaneshaAdapter
from arca_storage.adapters.lvm import SubprocessLVMAdapter
from arca_storage.adapters.netns import SubprocessNetNSAdapter
from arca_storage.adapters.pacemaker import SubprocessPacemakerAdapter
from arca_storage.adapters.systemd import SubprocessSystemdAdapter
from arca_storage.adapters.xfs import SubprocessXFSAdapter
from arca_storage.config import ArcaSettings, load_settings
from arca_storage.db import StateDB
from arca_storage.reconcilers.adapters import Adapters
from arca_storage.reconcilers.export import ExportReconciler
from arca_storage.reconcilers.snapshot import SnapshotReconciler
from arca_storage.reconcilers.svm import SVMReconciler
from arca_storage.reconcilers.volume import VolumeReconciler


class AppContext:
    """Singleton-ish container for all application dependencies."""

    def __init__(self, settings: Optional[ArcaSettings] = None) -> None:
        self.settings = settings or load_settings()
        self.db = StateDB(self.settings.state.db_path)

        t = self.settings.timeouts
        self.adapters = Adapters(
            lvm=SubprocessLVMAdapter(timeout=t.subprocess_default),
            xfs=SubprocessXFSAdapter(timeout=t.subprocess_default),
            netns=SubprocessNetNSAdapter(timeout=t.subprocess_default),
            pacemaker=SubprocessPacemakerAdapter(timeout=t.pacemaker_operation),
            ganesha=SubprocessGaneshaAdapter(
                timeout=t.subprocess_default, settings=self.settings
            ),
            systemd=SubprocessSystemdAdapter(timeout=t.subprocess_default),
        )

        cfg = dict(self.settings.to_reconciler_config())
        self.svm_reconciler = SVMReconciler(self.db, self.adapters, config=cfg)
        self.volume_reconciler = VolumeReconciler(self.db, self.adapters, config=cfg)
        self.snapshot_reconciler = SnapshotReconciler(
            self.db, self.adapters, config=cfg
        )
        self.export_reconciler = ExportReconciler(self.db, self.adapters, config=cfg)

    def close(self) -> None:
        self.db.close()


# Module-level lazy singleton
_ctx: Optional[AppContext] = None


def get_context() -> AppContext:
    global _ctx
    if _ctx is None:
        _ctx = AppContext()
    return _ctx


def reset_context(ctx: Optional[AppContext] = None) -> None:
    """Replace the global context (useful for testing)."""
    global _ctx
    old_ctx = _ctx
    _ctx = ctx
    if old_ctx is not None and old_ctx is not ctx:
        old_ctx.close()
