"""
Snapshot Reconciler — drives snapshot resources from desired to actual state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from math import ceil
from typing import Optional

from arca_storage.cli.lib.validators import legacy_snapshot_lv_name, legacy_volume_lv_name, snapshot_lv_name
from arca_storage.create_resume import clear_create_lease
from arca_storage.db import StateDB
from arca_storage.errors import CreateLeaseLostError, PreconditionFailedError
from arca_storage.models.base import Phase
from arca_storage.models.snapshot import Snapshot
from arca_storage.reconcilers.adapters import Adapters
from arca_storage.reconcilers.lvm_resume import create_snapshot_lv_or_accept_existing_with_result

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
            if not self._is_failed_delete(snapshot) and not snapshot.status.lv_created:
                snapshot.status.phase = Phase.CREATING
                return self._reconcile_create(snapshot)
            return snapshot
        elif phase == Phase.READY:
            return snapshot
        return snapshot

    def _reconcile_create(self, snapshot: Snapshot) -> Snapshot:
        snapshot.status.phase = Phase.CREATING
        create_owner = snapshot.status.create_owner
        spec = snapshot.spec
        vg_name = self._cfg.get("vg_name", "vg_pool_01")
        source_lv = self._source_volume_lv_name(spec.svm, spec.volume)
        snap_lv = snapshot.status.lv_name or snapshot_lv_name(spec.svm, spec.volume, spec.name)
        snapshot.status.lv_name = snap_lv

        if not snapshot.status.lv_created:
            created_snap_lv = False
            try:
                snap_result = create_snapshot_lv_or_accept_existing_with_result(
                    self.adapters.lvm,
                    vg_name,
                    source_lv,
                    snap_lv,
                )
                created_snap_lv = snap_result.created
                snap_path = snap_result.path
                snap_size_gib = self._snapshot_lv_size_gib(vg_name, snap_lv)
                snapshot.status.lv_created = True
                snapshot.status.lv_path = snap_path
                snapshot.status.lv_name = snap_lv
                snapshot.status.size_gib = snap_size_gib
                self._persist(
                    snapshot,
                    "snapshot LV created",
                    expected_create_owner=create_owner,
                    require_ready_volume=True,
                )
            except CreateLeaseLostError:
                if created_snap_lv:
                    self._delete_created_snapshot_lv_if_untracked(snapshot, vg_name, snap_lv)
                raise
            except PreconditionFailedError:
                if created_snap_lv:
                    self._delete_created_snapshot_lv(vg_name, snap_lv)
                raise
            except Exception as e:
                if created_snap_lv:
                    self._delete_created_snapshot_lv(vg_name, snap_lv)
                    snapshot.status.lv_created = False
                    snapshot.status.lv_path = None
                    snapshot.status.size_gib = None
                snapshot.status.phase = Phase.FAILED
                expected_owner = create_owner
                clear_create_lease(snapshot.status)
                snapshot.status.message = f"Create failed: {e}"
                self._persist(snapshot, snapshot.status.message, expected_create_owner=expected_owner)
                logger.error("Snapshot %s/%s/%s create failed: %s", spec.svm, spec.volume, spec.name, e)
                return snapshot

        if snapshot.status.size_gib is None:
            try:
                snapshot.status.size_gib = self._snapshot_lv_size_gib(vg_name, snap_lv)
            except Exception as e:
                snapshot.status.phase = Phase.FAILED
                expected_owner = create_owner
                clear_create_lease(snapshot.status)
                snapshot.status.message = f"Create failed: {e}"
                self._persist(snapshot, snapshot.status.message, expected_create_owner=expected_owner)
                logger.error("Snapshot %s/%s/%s create failed: %s", spec.svm, spec.volume, spec.name, e)
                return snapshot

        snapshot.status.phase = Phase.READY
        expected_owner = create_owner
        clear_create_lease(snapshot.status)
        snapshot.status.message = ""
        snapshot.status.last_reconciled = datetime.now(timezone.utc)
        self._persist(
            snapshot,
            "Snapshot ready",
            expected_create_owner=expected_owner,
            require_ready_volume=True,
        )
        return snapshot

    def _reconcile_delete(self, snapshot: Snapshot) -> Snapshot:
        spec = snapshot.spec
        vg_name = self._cfg.get("vg_name", "vg_pool_01")
        snap_lv = snapshot.status.lv_name or legacy_snapshot_lv_name(spec.svm, spec.volume, spec.name)
        try:
            snapshot.status.phase = Phase.DELETING
            self._persist(snapshot, "snapshot delete reserved")
            self.adapters.lvm.delete_lv(vg_name, snap_lv)
            self.db.delete_snapshot(spec.svm, spec.volume, spec.name)
            self.db.log_operation("Snapshot", snapshot.metadata.id, "delete", "completed")
        except Exception as e:
            snapshot.status.phase = Phase.FAILED
            snapshot.status.message = f"Delete failed: {e}"
            self._persist(snapshot, snapshot.status.message)
        return snapshot

    def _persist(
        self,
        snapshot: Snapshot,
        detail: str,
        *,
        expected_create_owner: Optional[str] = None,
        require_ready_volume: bool = False,
    ) -> None:
        if not self.db.upsert_snapshot(
            snapshot,
            expected_create_owner=expected_create_owner,
            require_ready_volume=require_ready_volume,
            require_ready_svm=require_ready_volume,
        ):
            raise CreateLeaseLostError("Snapshot", f"{snapshot.spec.svm}/{snapshot.spec.volume}/{snapshot.spec.name}")
        self.db.log_operation(
            "Snapshot", snapshot.metadata.id, "reconcile", snapshot.status.phase.value, detail
        )

    @staticmethod
    def _is_failed_delete(snapshot: Snapshot) -> bool:
        return snapshot.status.message.startswith("Delete failed:")

    def _snapshot_lv_size_gib(self, vg_name: str, snap_lv: str) -> int:
        try:
            return int(ceil(float(self.adapters.lvm.get_lv_size_gib(vg_name, snap_lv))))
        except Exception as e:
            logger.warning("Failed to read snapshot size for %s/%s: %s", vg_name, snap_lv, e)
            raise RuntimeError("Snapshot size is unavailable") from e

    def _source_volume_lv_name(self, svm: str, volume: str) -> str:
        record = self.db.get_volume(svm, volume)
        if record is None:
            raise PreconditionFailedError(
                f"Volume '{svm}/{volume}' is not ready",
                {"resource": "Volume", "name": f"{svm}/{volume}", "phase": ""},
            )
        return str(record.get("status", {}).get("lv_name") or legacy_volume_lv_name(svm, volume))

    def _delete_created_snapshot_lv(self, vg_name: str, snap_lv: str) -> bool:
        try:
            self.adapters.lvm.delete_lv(vg_name, snap_lv)
            return True
        except Exception as e:
            logger.warning("Failed to delete unrecorded snapshot LV %s/%s: %s", vg_name, snap_lv, e)
            return False

    def _delete_created_snapshot_lv_if_untracked(self, snapshot: Snapshot, vg_name: str, snap_lv: str) -> None:
        cleanup_owner = snapshot.status.create_owner or snapshot.metadata.id
        try:
            reserved = self.db.reserve_snapshot_cleanup(
                snapshot.spec.svm,
                snapshot.spec.volume,
                snapshot.spec.name,
                cleanup_owner,
            )
        except Exception as e:
            logger.warning("Skipping snapshot LV cleanup after lost lease for %s/%s: %s", vg_name, snap_lv, e)
            return

        if not reserved:
            logger.info("Keeping snapshot LV %s/%s because the snapshot record is tracked or cleanup is reserved", vg_name, snap_lv)
            return

        if self._delete_created_snapshot_lv(vg_name, snap_lv):
            try:
                self.db.release_snapshot_cleanup(
                    snapshot.spec.svm,
                    snapshot.spec.volume,
                    snapshot.spec.name,
                    cleanup_owner,
                )
            except Exception as e:
                logger.warning("Failed to release snapshot LV cleanup reservation for %s/%s: %s", vg_name, snap_lv, e)
