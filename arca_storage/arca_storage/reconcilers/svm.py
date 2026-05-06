"""
SVM Reconciler — drives SVM resources from desired to actual state.

Each reconcile() call is idempotent: it inspects status flags and only
executes the next pending step. On failure, the SVM moves to FAILED
phase with a message; a subsequent reconcile retries from the last
successful step.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from arca_storage.cli.lib.netns import allocate_vlan_ifname
from arca_storage.cli.lib.validators import infer_gateway_from_ip_cidr, validate_ip_cidr
from arca_storage.db import StateDB
from arca_storage.models.base import Phase
from arca_storage.models.svm import SVM, SVMSpec
from arca_storage.reconcilers.adapters import Adapters

logger = logging.getLogger(__name__)


class SVMReconciler:
    def __init__(self, db: StateDB, adapters: Adapters, *, config: Optional[dict] = None) -> None:
        self.db = db
        self.adapters = adapters
        self._cfg = config or {}

    # ---- public API ----

    def reconcile(self, svm: SVM) -> SVM:
        """Idempotent reconciliation — safe to call repeatedly."""
        phase = svm.status.phase
        if phase in (Phase.PENDING, Phase.CREATING):
            return self._reconcile_create(svm)
        elif phase == Phase.DELETING:
            return self._reconcile_delete(svm)
        elif phase == Phase.READY:
            return self._reconcile_drift(svm)
        elif phase == Phase.FAILED:
            if not self._is_failed_delete(svm) and self._has_pending_create_step(svm):
                svm.status.phase = Phase.CREATING
                return self._reconcile_create(svm)
            return svm  # failed delete or completed resource needs manual intervention
        return svm

    # ---- create ----

    def _reconcile_create(self, svm: SVM) -> SVM:
        svm.status.phase = Phase.CREATING
        spec = svm.spec

        ip_addr, prefix = validate_ip_cidr(spec.ip_cidr)
        uses_vlan = spec.vlan_id is not None
        gateway = spec.gateway or (infer_gateway_from_ip_cidr(spec.ip_cidr) if uses_vlan else None)
        parent_if = self._cfg.get("parent_if", "bond0")
        vg_name = self._cfg.get("vg_name", "vg_pool_01")
        thinpool = self._cfg.get("thinpool_name", "pool")
        export_dir = self._cfg.get("export_dir", "/exports")
        drbd_resource = self._cfg.get("drbd_resource", "r0")

        steps = []
        if uses_vlan:
            steps.extend([
                (
                    "namespace_created",
                    lambda: self.adapters.netns.create_namespace(spec.name),
                ),
                (
                    "vlan_attached",
                    lambda: self._attach_vlan(svm, parent_if, gateway),
                ),
            ])
        steps.append(
            (
                "ganesha_configured",
                lambda: self.adapters.ganesha.render_config(
                    spec.name,
                    [],
                    bind_addr=ip_addr,
                    host_network=not uses_vlan,
                ),
            )
        )

        if spec.root_volume_size_gib:
            lv_name = f"vol_{spec.name}"
            lv_path = f"/dev/{vg_name}/{lv_name}"
            steps.append((
                "lv_created",
                lambda: self.adapters.lvm.create_thin_lv(vg_name, thinpool, lv_name, spec.root_volume_size_gib),
            ))
            steps.append((
                "fs_formatted",
                lambda: self.adapters.xfs.format_xfs(lv_path),
            ))

        steps.append((
            "pacemaker_group_created",
            lambda: self.adapters.pacemaker.create_group(
                spec.name,
                f"{export_dir}/{spec.name}",
                vlan_id=spec.vlan_id,
                ifname=svm.status.vlan_ifname,
                ip=ip_addr,
                prefix=prefix,
                gw=gateway,
                mtu=spec.mtu,
                parent_if=parent_if,
                vg_name=vg_name,
                create_filesystem=bool(spec.root_volume_size_gib),
                drbd_resource_name=drbd_resource,
                enforce_drbd_constraints=True,
            ),
        ))

        for field, action in steps:
            if getattr(svm.status, field, False):
                continue  # already done
            try:
                action()
                setattr(svm.status, field, True)
                self._persist(svm, f"step '{field}' completed")
            except Exception as e:
                svm.status.phase = Phase.FAILED
                svm.status.message = f"Step '{field}' failed: {e}"
                self._persist(svm, svm.status.message)
                logger.error("SVM %s reconcile failed at %s: %s", spec.name, field, e)
                return svm

        svm.status.phase = Phase.READY
        svm.status.message = ""
        svm.status.last_reconciled = datetime.now(timezone.utc)
        self._persist(svm, "SVM ready")
        return svm

    def _attach_vlan(self, svm: SVM, parent_if: str, gateway: str) -> None:
        if svm.spec.vlan_id is None:
            raise ValueError("vlan_id is required to attach a VLAN interface")
        ifname = svm.status.vlan_ifname
        if not ifname:
            ifname = allocate_vlan_ifname(svm.spec.name, svm.spec.vlan_id)
            svm.status.vlan_ifname = ifname
        self.adapters.netns.attach_vlan(
            svm.spec.name,
            parent_if,
            svm.spec.vlan_id,
            svm.spec.ip_cidr,
            gateway,
            svm.spec.mtu,
            ifname,
        )

    # ---- delete ----

    def _reconcile_delete(self, svm: SVM) -> SVM:
        spec = svm.spec
        vg_name = self._cfg.get("vg_name", "vg_pool_01")
        try:
            self.adapters.pacemaker.delete_group(spec.name)
            self.adapters.netns.delete_namespace(spec.name)
            if spec.root_volume_size_gib or svm.status.lv_created:
                self.adapters.lvm.delete_lv(vg_name, f"vol_{spec.name}")
            self.db.delete_svm(spec.name)
            self.db.log_operation("SVM", svm.metadata.id, "delete", "completed")
        except Exception as e:
            svm.status.phase = Phase.FAILED
            svm.status.message = f"Delete failed: {e}"
            self._persist(svm, svm.status.message)
            logger.error("SVM %s delete failed: %s", spec.name, e)
        return svm

    # ---- drift detection (placeholder) ----

    def _reconcile_drift(self, svm: SVM) -> SVM:
        svm.status.last_reconciled = datetime.now(timezone.utc)
        return svm

    # ---- helpers ----

    def _persist(self, svm: SVM, detail: str) -> None:
        self.db.upsert_svm(svm)
        self.db.log_operation("SVM", svm.metadata.id, "reconcile", svm.status.phase.value, detail)

    @staticmethod
    def _has_pending_create_step(svm: SVM) -> bool:
        fields = ["ganesha_configured", "pacemaker_group_created"]
        if svm.spec.vlan_id is not None:
            fields.extend(["namespace_created", "vlan_attached"])
        if svm.spec.root_volume_size_gib:
            fields.extend(["lv_created", "fs_formatted"])
        return any(not getattr(svm.status, field, False) for field in fields)

    @staticmethod
    def _is_failed_delete(svm: SVM) -> bool:
        return svm.status.message.startswith("Delete failed:")
