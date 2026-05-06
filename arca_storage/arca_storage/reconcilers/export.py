"""
Export Reconciler — drives NFS export resources from desired to actual state.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from ipaddress import IPv4Interface
from typing import Optional

from arca_storage.create_resume import ACTIVE_CREATE_PHASES, is_stale_create_reservation
from arca_storage.db import StateDB
from arca_storage.errors import AlreadyExistsError
from arca_storage.models.base import Phase, ResourceMeta
from arca_storage.models.export import Export, ExportSpec, ExportStatus
from arca_storage.reconcilers.adapters import Adapters

logger = logging.getLogger(__name__)


class ExportReconciler:
    def __init__(self, db: StateDB, adapters: Adapters, *, config: Optional[dict] = None) -> None:
        self.db = db
        self.adapters = adapters
        self._cfg = config or {}

    def reconcile(self, export: Export, *, allow_update: bool = False) -> Export:
        phase = export.status.phase
        if phase in (Phase.PENDING, Phase.CREATING):
            return self._reconcile_create(export, allow_update=allow_update)
        elif phase == Phase.DELETING:
            return self._reconcile_delete(export)
        elif phase == Phase.FAILED:
            if not self._is_failed_delete(export) and self._has_pending_create_step(export):
                export.status.phase = Phase.CREATING
                return self._reconcile_create(export, allow_update=allow_update)
            return export
        elif phase == Phase.READY:
            return export
        return export

    def _reconcile_create(self, export: Export, *, allow_update: bool = False) -> Export:
        export.status.phase = Phase.CREATING
        spec = export.spec
        export_dir = self._cfg.get("export_dir", "/exports")

        with self.db.transaction(immediate=True) as conn:
            existing = self.db._get_export_conn(conn, spec.svm, spec.volume, spec.client)
            if existing:
                if not allow_update and not _can_resume_create_record(existing, spec):
                    raise AlreadyExistsError("Export", f"{spec.svm}/{spec.volume}/{spec.client}")
                export.metadata = _meta_from_record(existing)
                previous_status = ExportStatus.model_validate(existing["status"])
                export.status.export_id = previous_status.export_id

            if export.status.export_id is None:
                records = self.db._list_exports_conn(conn, svm=spec.svm, limit=_all_rows_limit())
                export.status.export_id = _next_export_id(records)

            export.status.path = _export_path(spec, export_dir)
            export.status.pseudo = _export_pseudo(spec, export.status.path)
            export.status.ganesha_configured = False
            export.status.service_reloaded = False
            export.status.message = ""
            self._persist_conn(conn, export, "export state reserved")

            config_entries = self._config_entries_for_svm(
                conn,
                spec.svm,
                export_dir,
                include_pending_key=(spec.svm, spec.volume, spec.client),
            )
            bind_addr, host_network = self._ganesha_network_for_svm(conn, spec.svm)
            try:
                self.adapters.ganesha.render_config(
                    spec.svm,
                    config_entries,
                    bind_addr=bind_addr,
                    host_network=host_network,
                )
                export.status.ganesha_configured = True
                self._persist_conn(conn, export, "ganesha config rendered")
            except Exception as e:
                self._rollback_svm_config(conn, spec.svm, export_dir, host_network=host_network)
                export.status.phase = Phase.FAILED
                export.status.message = f"Config failed: {e}"
                self._persist_conn(conn, export, export.status.message)
                return export

            try:
                self.adapters.ganesha.reload(spec.svm, host_network=host_network)
                export.status.service_reloaded = True
                self._persist_conn(conn, export, "ganesha reloaded")
            except Exception as e:
                self._rollback_svm_config(conn, spec.svm, export_dir, host_network=host_network)
                export.status.phase = Phase.FAILED
                export.status.message = f"Reload failed: {e}"
                self._persist_conn(conn, export, export.status.message)
                return export

            export.status.phase = Phase.READY
            export.status.message = ""
            export.status.last_reconciled = datetime.now(timezone.utc)
            self._persist_conn(conn, export, "Export ready")
        return export

    def _reconcile_delete(self, export: Export) -> Export:
        spec = export.spec
        export_dir = self._cfg.get("export_dir", "/exports")

        with self.db.transaction(immediate=True) as conn:
            existing = self.db._get_export_conn(conn, spec.svm, spec.volume, spec.client)
            if not existing:
                self.db._log_operation_conn(conn, "Export", export.metadata.id, "delete", "not_found")
                return export

            export = Export(
                metadata=_meta_from_record(existing),
                spec=ExportSpec.model_validate(existing["spec"]),
                status=ExportStatus.model_validate(existing["status"]),
            )
            export.status.phase = Phase.DELETING

            remaining_records = [
                r
                for r in self.db._list_exports_conn(conn, svm=spec.svm, limit=_all_rows_limit())
                if not (
                    r.get("spec", {}).get("svm") == spec.svm
                    and r.get("spec", {}).get("volume") == spec.volume
                    and r.get("spec", {}).get("client") == spec.client
                )
            ]
            config_entries = _records_to_config_entries(remaining_records, export_dir)
            bind_addr, host_network = self._ganesha_network_for_svm(conn, spec.svm)
            try:
                self.adapters.ganesha.render_config(
                    spec.svm,
                    config_entries,
                    bind_addr=bind_addr,
                    host_network=host_network,
                )
                self.adapters.ganesha.reload(spec.svm, host_network=host_network)
                self.db._delete_export_conn(conn, spec.svm, spec.volume, spec.client)
                self.db._log_operation_conn(conn, "Export", export.metadata.id, "delete", "completed")
            except Exception as e:
                self._rollback_svm_config(conn, spec.svm, export_dir, host_network=host_network)
                export.status.phase = Phase.FAILED
                export.status.message = f"Delete failed: {e}"
                self._persist_conn(conn, export, export.status.message)
        return export

    def _persist_conn(self, conn, export: Export, detail: str) -> None:
        self.db._upsert_export_conn(conn, export)
        self.db._log_operation_conn(
            conn, "Export", export.metadata.id, "reconcile", export.status.phase.value, detail
        )

    def _config_entries_for_svm(
        self,
        conn,
        svm_name: str,
        export_dir: str,
        *,
        include_pending_key: tuple[str, str, str] | None = None,
    ) -> list[dict]:
        records = self.db._list_exports_conn(conn, svm=svm_name, limit=_all_rows_limit())
        return _records_to_config_entries(records, export_dir, include_pending_key=include_pending_key)

    def sync_svm_config(self, svm_name: str) -> str:
        """Render and reload one SVM config from DB-backed exports."""
        export_dir = self._cfg.get("export_dir", "/exports")
        with self.db.transaction(immediate=True) as conn:
            config_entries = self._config_entries_for_svm(conn, svm_name, export_dir)
            bind_addr, host_network = self._ganesha_network_for_svm(conn, svm_name)
            path = self.adapters.ganesha.render_config(
                svm_name,
                config_entries,
                bind_addr=bind_addr,
                host_network=host_network,
            )
            self.adapters.ganesha.reload(svm_name, host_network=host_network)
            return path

    def _rollback_svm_config(self, conn, svm_name: str, export_dir: str, *, host_network: bool) -> None:
        """Best-effort restore of the active config to DB-ready exports."""
        try:
            config_entries = self._config_entries_for_svm(conn, svm_name, export_dir)
            bind_addr, _ = self._ganesha_network_for_svm(conn, svm_name)
            self.adapters.ganesha.render_config(
                svm_name,
                config_entries,
                bind_addr=bind_addr,
                host_network=host_network,
            )
        except Exception as rollback_error:
            logger.warning("Failed to roll back Ganesha config for SVM %s: %s", svm_name, rollback_error)

    def _ganesha_network_for_svm(self, conn, svm_name: str) -> tuple[Optional[str], bool]:
        record = conn.execute("SELECT spec FROM svms WHERE name = ?", (svm_name,)).fetchone()
        if not record:
            return None, False

        spec = json.loads(record["spec"])
        ip_cidr = str(spec.get("ip_cidr") or "")
        try:
            bind_addr = str(IPv4Interface(ip_cidr).ip)
        except Exception:
            bind_addr = ip_cidr.split("/", 1)[0] if ip_cidr else None
        return bind_addr, spec.get("vlan_id") is None

    @staticmethod
    def _has_pending_create_step(export: Export) -> bool:
        return not export.status.ganesha_configured or not export.status.service_reloaded

    @staticmethod
    def _is_failed_delete(export: Export) -> bool:
        return export.status.message.startswith("Delete failed:")


def _records_to_config_entries(
    records: list[dict],
    export_dir: str,
    *,
    include_pending_key: tuple[str, str, str] | None = None,
) -> list[dict]:
    entries = []
    for record in records:
        spec = record.get("spec", {})
        status = record.get("status", {})
        export_id = status.get("export_id")
        if export_id is None:
            continue
        phase = status.get("phase")
        key = (spec.get("svm"), spec.get("volume"), spec.get("client"))
        is_current_pending_create = phase == Phase.CREATING.value and key == include_pending_key
        if phase != Phase.READY.value and not is_current_pending_create:
            continue

        path = status.get("path") or spec.get("path") or _export_path(ExportSpec.model_validate(spec), export_dir)
        entry = {
            "export_id": int(export_id),
            "path": path,
            "pseudo": status.get("pseudo") or spec.get("pseudo") or path,
            "access": str(spec.get("access", "RW")).upper(),
            "squash": "Root_Squash" if spec.get("root_squash", True) else "No_Root_Squash",
            "sec": spec.get("sec") or ["sys"],
            "client": spec.get("client"),
        }
        if spec.get("owner"):
            entry["owner"] = spec.get("owner")
        entries.append(entry)

    return sorted(entries, key=lambda e: (int(e.get("export_id") or 0), str(e.get("path") or "")))


def _next_export_id(records: list[dict]) -> int:
    export_ids = []
    for record in records:
        raw = record.get("status", {}).get("export_id")
        if raw is not None:
            export_ids.append(int(raw))
    return max(export_ids, default=0) + 1


def _can_resume_create_record(record: dict, requested_spec: ExportSpec) -> bool:
    status = record.get("status", {})
    phase = status.get("phase")
    if ExportSpec.model_validate(record["spec"]) != requested_spec:
        return False
    if phase in ACTIVE_CREATE_PHASES:
        return is_stale_create_reservation(record)
    if phase != Phase.FAILED.value:
        return False
    if str(status.get("message") or "").startswith("Delete failed:"):
        return False
    return not status.get("ganesha_configured", False) or not status.get("service_reloaded", False)


def _export_path(spec: ExportSpec, export_dir: str) -> str:
    if spec.path:
        return spec.path
    return f"{str(export_dir).rstrip('/')}/{spec.svm}/{spec.volume}"


def _export_pseudo(spec: ExportSpec, path: str) -> str:
    return spec.pseudo or path


def _meta_from_record(record: dict) -> ResourceMeta:
    return ResourceMeta(id=record["id"], generation=record.get("generation", 1))


def _all_rows_limit() -> int:
    return 1_000_000
