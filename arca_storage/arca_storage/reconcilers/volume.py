"""
Volume Reconciler — drives volume resources from desired to actual state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from arca_storage.create_resume import clear_create_lease
from arca_storage.cli.lib.validators import legacy_volume_lv_name, volume_lv_name
from arca_storage.db import StateDB
from arca_storage.errors import CreateLeaseLostError
from arca_storage.models.base import Phase
from arca_storage.models.volume import Volume
from arca_storage.reconcilers.adapters import Adapters
from arca_storage.reconcilers.lvm_resume import create_volume_lv_or_accept_existing

logger = logging.getLogger(__name__)


class VolumeReconciler:
    def __init__(
        self, db: StateDB, adapters: Adapters, *, config: Optional[dict] = None
    ) -> None:
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
            if not self._is_failed_delete(volume) and self._has_pending_create_step(
                volume
            ):
                volume.status.phase = Phase.CREATING
                return self._reconcile_create(volume)
            return volume
        elif phase == Phase.READY:
            return volume
        return volume

    def _reconcile_create(self, volume: Volume) -> Volume:
        volume.status.phase = Phase.CREATING
        create_owner = volume.status.create_owner
        spec = volume.spec

        vg_name = self._cfg.get("vg_name", "vg_pool_01")
        thinpool = self._cfg.get("thinpool_name", "pool")
        export_dir = self._cfg.get("export_dir", "/exports")
        lv_name = volume.status.lv_name or volume_lv_name(spec.svm, spec.name)
        volume.status.lv_name = lv_name
        lv_path = f"/dev/{vg_name}/{lv_name}"
        default_mount_path = f"{export_dir}/{spec.svm}/{spec.name}"
        mount_path = volume.status.mount_path or default_mount_path
        self._reset_missing_create_resources(
            volume, vg_name, lv_name, mount_path, create_owner
        )
        mount_path = volume.status.mount_path or default_mount_path

        steps = [
            (
                "lv_created",
                lambda: create_volume_lv_or_accept_existing(
                    self.adapters.lvm,
                    vg_name,
                    thinpool,
                    lv_name,
                    spec.size_gib,
                    thin=spec.thin,
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
                self._persist(
                    volume,
                    f"step '{field}' completed",
                    expected_create_owner=create_owner,
                )
            except CreateLeaseLostError:
                raise
            except Exception as e:
                volume.status.phase = Phase.FAILED
                expected_owner = create_owner
                clear_create_lease(volume.status)
                volume.status.message = f"Step '{field}' failed: {e}"
                self._persist(
                    volume, volume.status.message, expected_create_owner=expected_owner
                )
                logger.error(
                    "Volume %s/%s reconcile failed at %s: %s",
                    spec.svm,
                    spec.name,
                    field,
                    e,
                )
                return volume

        volume.status.phase = Phase.READY
        expected_owner = create_owner
        clear_create_lease(volume.status)
        volume.status.message = ""
        volume.status.last_reconciled = datetime.now(timezone.utc)
        self._persist(volume, "Volume ready", expected_create_owner=expected_owner)
        return volume

    def _reset_missing_create_resources(
        self,
        volume: Volume,
        vg_name: str,
        lv_name: str,
        mount_path: str,
        create_owner: Optional[str],
    ) -> None:
        changed = False
        if volume.status.lv_created and not self.adapters.lvm.lv_exists(
            vg_name, lv_name
        ):
            volume.status.lv_created = False
            volume.status.lv_path = None
            volume.status.fs_formatted = False
            volume.status.mounted = False
            volume.status.mount_path = None
            changed = True

        if volume.status.mounted:
            try:
                mounted = self.adapters.xfs.is_mounted(mount_path)
            except Exception:
                mounted = True
            if not mounted:
                volume.status.mounted = False
                changed = True

        if changed:
            self._persist(
                volume, "Volume create state reset", expected_create_owner=create_owner
            )

    def _reconcile_delete(self, volume: Volume) -> Volume:
        spec = volume.spec
        vg_name = self._cfg.get("vg_name", "vg_pool_01")
        export_dir = self._cfg.get("export_dir", "/exports")
        lv_name = volume.status.lv_name or legacy_volume_lv_name(spec.svm, spec.name)
        mount_path = volume.status.mount_path or f"{export_dir}/{spec.svm}/{spec.name}"

        try:
            volume.status.phase = Phase.DELETING
            self._persist(volume, "volume delete reserved")
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

    def _persist(
        self,
        volume: Volume,
        detail: str,
        *,
        expected_create_owner: Optional[str] = None,
    ) -> None:
        if not self.db.upsert_volume(
            volume, expected_create_owner=expected_create_owner
        ):
            raise CreateLeaseLostError(
                "Volume", f"{volume.spec.svm}/{volume.spec.name}"
            )
        self.db.log_operation(
            "Volume", volume.metadata.id, "reconcile", volume.status.phase.value, detail
        )

    @staticmethod
    def _has_pending_create_step(volume: Volume) -> bool:
        return any(
            not getattr(volume.status, field, False)
            for field in ("lv_created", "fs_formatted", "mounted")
        )

    @staticmethod
    def _is_failed_delete(volume: Volume) -> bool:
        return volume.status.message.startswith("Delete failed:")
