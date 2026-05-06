"""
Snapshot Reconciler — drives snapshot resources from desired to actual state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from arca_storage.db import StateDB
from arca_storage.models.base import Phase
from arca_storage.models.snapshot import Snapshot
from arca_storage.reconcilers.adapters import Adapters

logger = logging.getLogger(__name__)


class SnapshotReconciler:
    def __init__(self, db: StateDB, adapters: Adapters, *, config: Optional[dict] = None) -> None:
        self.db = db
        self.adapters = adapters
        self._cfg = config or {}

    def reconcile(self, snapshot: Snapshot) -> Snapshot:
        phase = snapshot.status.phase
        if phase in (Phase.PENDING, Phase.CREATING):
            return self._reconcile_create(snapshot)
        elif phase == Phase.DELETING:
            return self._reconcile_delete(snapshot)
        elif phase == Phase.FAILED:
            if not snapshot.status.lv_created:
                snapshot.status.phase = Phase.CREATING
                return self._reconcile_create(snapshot)
            return snapshot
        elif phase == Phase.READY:
            return snapshot
        return snapshot

    def _reconcile_create(self, snapshot: Snapshot) -> Snapshot:
        snapshot.status.phase = Phase.CREATING
        spec = snapshot.spec
        vg_name = self._cfg.get("vg_name", "vg_pool_01")
        source_lv = f"vol_{spec.svm}_{spec.volume}"
        snap_lv = f"vol_{spec.svm}_{spec.volume}_snap_{spec.name}"

        if not snapshot.status.lv_created:
            try:
                snap_path = self.adapters.lvm.create_snapshot(vg_name, source_lv, snap_lv)
                snapshot.status.lv_created = True
                snapshot.status.lv_path = snap_path
                snapshot.status.lv_name = snap_lv
                self._persist(snapshot, "snapshot LV created")
            except Exception as e:
                snapshot.status.phase = Phase.FAILED
                snapshot.status.message = f"Create failed: {e}"
                self._persist(snapshot, snapshot.status.message)
                logger.error("Snapshot %s/%s/%s create failed: %s", spec.svm, spec.volume, spec.name, e)
                return snapshot

        snapshot.status.phase = Phase.READY
        snapshot.status.message = ""
        snapshot.status.last_reconciled = datetime.now(timezone.utc)
        self._persist(snapshot, "Snapshot ready")
        return snapshot

    def _reconcile_delete(self, snapshot: Snapshot) -> Snapshot:
        spec = snapshot.spec
        vg_name = self._cfg.get("vg_name", "vg_pool_01")
        snap_lv = f"vol_{spec.svm}_{spec.volume}_snap_{spec.name}"
        try:
            self.adapters.lvm.delete_lv(vg_name, snap_lv)
            self.db.delete_snapshot(spec.svm, spec.volume, spec.name)
            self.db.log_operation("Snapshot", snapshot.metadata.id, "delete", "completed")
        except Exception as e:
            snapshot.status.phase = Phase.FAILED
            snapshot.status.message = f"Delete failed: {e}"
            self._persist(snapshot, snapshot.status.message)
        return snapshot

    def _persist(self, snapshot: Snapshot, detail: str) -> None:
        self.db.upsert_snapshot(snapshot)
        self.db.log_operation(
            "Snapshot", snapshot.metadata.id, "reconcile", snapshot.status.phase.value, detail
        )
