"""
Volume Reconciler — drives volume resources from desired to actual state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from arca_storage.db import StateDB
from arca_storage.models.base import Phase
from arca_storage.models.volume import Volume
from arca_storage.reconcilers.adapters import Adapters

logger = logging.getLogger(__name__)


class VolumeReconciler:
    def __init__(self, db: StateDB, adapters: Adapters, *, config: Optional[dict] = None) -> None:
        self.db = db
        self.adapters = adapters
        self._cfg = config or {}

    def reconcile(self, volume: Volume) -> Volume:
        phase = volume.status.phase
        if phase in (Phase.PENDING, Phase.CREATING):
            return self._reconcile_create(volume)
        elif phase == Phase.DELETING:
            return self._reconcile_delete(volume)
        elif phase == Phase.FAILED:
            if self._has_pending_create_step(volume):
                volume.status.phase = Phase.CREATING
                return self._reconcile_create(volume)
            return volume
        elif phase == Phase.READY:
            return volume
        return volume

    def _reconcile_create(self, volume: Volume) -> Volume:
        volume.status.phase = Phase.CREATING
        spec = volume.spec

        vg_name = self._cfg.get("vg_name", "vg_pool_01")
        thinpool = self._cfg.get("thinpool_name", "pool")
        export_dir = self._cfg.get("export_dir", "/exports")
        lv_name = f"vol_{spec.svm}_{spec.name}"
        lv_path = f"/dev/{vg_name}/{lv_name}"
        mount_path = f"{export_dir}/{spec.svm}/{spec.name}"

        steps = [
            (
                "lv_created",
                lambda: (
                    self.adapters.lvm.create_thin_lv(vg_name, thinpool, lv_name, spec.size_gib)
                    if spec.thin
                    else self.adapters.lvm.create_regular_lv(vg_name, lv_name, spec.size_gib)
                ),
            ),
            ("fs_formatted", lambda: self.adapters.xfs.format_xfs(lv_path)),
            ("mounted", lambda: self.adapters.xfs.mount(lv_path, mount_path)),
        ]

        for field, action in steps:
            if getattr(volume.status, field, False):
                continue
            try:
                action()
                setattr(volume.status, field, True)
                # Record derived paths
                volume.status.lv_path = lv_path
                volume.status.lv_name = lv_name
                volume.status.mount_path = mount_path
                self._persist(volume, f"step '{field}' completed")
            except Exception as e:
                volume.status.phase = Phase.FAILED
                volume.status.message = f"Step '{field}' failed: {e}"
                self._persist(volume, volume.status.message)
                logger.error("Volume %s/%s reconcile failed at %s: %s", spec.svm, spec.name, field, e)
                return volume

        volume.status.phase = Phase.READY
        volume.status.message = ""
        volume.status.last_reconciled = datetime.now(timezone.utc)
        self._persist(volume, "Volume ready")
        return volume

    def _reconcile_delete(self, volume: Volume) -> Volume:
        spec = volume.spec
        vg_name = self._cfg.get("vg_name", "vg_pool_01")
        export_dir = self._cfg.get("export_dir", "/exports")
        lv_name = f"vol_{spec.svm}_{spec.name}"
        mount_path = f"{export_dir}/{spec.svm}/{spec.name}"

        try:
            self.adapters.xfs.umount(mount_path)
            self.adapters.lvm.delete_lv(vg_name, lv_name)
            self.db.delete_volume(spec.svm, spec.name)
            self.db.log_operation("Volume", volume.metadata.id, "delete", "completed")
        except Exception as e:
            volume.status.phase = Phase.FAILED
            volume.status.message = f"Delete failed: {e}"
            self._persist(volume, volume.status.message)
            logger.error("Volume %s/%s delete failed: %s", spec.svm, spec.name, e)
        return volume

    def _persist(self, volume: Volume, detail: str) -> None:
        self.db.upsert_volume(volume)
        self.db.log_operation("Volume", volume.metadata.id, "reconcile", volume.status.phase.value, detail)

    @staticmethod
    def _has_pending_create_step(volume: Volume) -> bool:
        return any(
            not getattr(volume.status, field, False)
            for field in ("lv_created", "fs_formatted", "mounted")
        )
