"""
Export Reconciler — drives NFS export resources from desired to actual state.
"""

from __future__ import annotations

import fcntl
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from arca_storage.cli.lib.validators import validate_svm_ip_cidr
from arca_storage.create_resume import ACTIVE_CREATE_PHASES, clear_create_lease
from arca_storage.db import StateDB
from arca_storage.errors import AlreadyExistsError, CreateLeaseLostError, PreconditionFailedError
from arca_storage.models.base import Phase, ResourceMeta, resource_meta_from_record
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
        create_owner = export.status.create_owner
        spec = export.spec
        export_dir = self._cfg.get("export_dir", "/exports")
        previous_ready_export: Optional[Export] = None
        require_ready_volume = _requires_ready_volume(export)
        require_ready_svm = True

        with self.db.transaction(immediate=True) as conn:
            existing = self.db._get_export_conn(conn, spec.svm, spec.volume, spec.client)
            if existing:
                if not allow_update and not _can_resume_create_record(existing, export):
                    raise AlreadyExistsError("Export", f"{spec.svm}/{spec.volume}/{spec.client}")
                previous_export = _export_from_record(existing)
                export.metadata = previous_export.metadata
                previous_status = ExportStatus.model_validate(existing["status"])
                export.status.export_id = previous_status.export_id
                if allow_update and previous_status.phase == Phase.READY:
                    previous_ready_export = previous_export

            if export.status.export_id is None:
                records = self.db._list_exports_conn(conn, svm=spec.svm, limit=_all_rows_limit())
                export.status.export_id = _next_export_id(records)

            export.status.path = _export_path(spec, export_dir)
            export.status.pseudo = _export_pseudo(spec, export.status.path)
            export.status.ganesha_configured = False
            export.status.service_reloaded = False
            export.status.message = ""
            self._persist_conn(
                conn,
                export,
                "export state reserved",
                expected_create_owner=create_owner,
                require_ready_volume=require_ready_volume,
                require_ready_svm=require_ready_svm,
                allow_missing_create_owner=True,
            )

        with self._svm_config_lock(spec.svm):
            config_entries, bind_addr, host_network = self._config_snapshot_for_svm(
                spec.svm,
                export_dir,
                include_transient_key=(spec.svm, spec.volume, spec.client),
            )
            try:
                self.adapters.ganesha.render_config(
                    spec.svm,
                    config_entries,
                    bind_addr=bind_addr,
                    host_network=host_network,
                )
                export.status.ganesha_configured = True
                self._persist(
                    export,
                    "ganesha config rendered",
                    expected_create_owner=create_owner,
                    require_ready_volume=require_ready_volume,
                    require_ready_svm=require_ready_svm,
                )
            except CreateLeaseLostError:
                raise
            except PreconditionFailedError:
                self._rollback_svm_config(spec.svm, export_dir, host_network=host_network)
                raise
            except Exception as e:
                return self._fail_create(
                    export,
                    f"Config failed: {e}",
                    export_dir,
                    host_network=host_network,
                    create_owner=create_owner,
                    previous_ready_export=previous_ready_export,
                )

            try:
                self.adapters.ganesha.reload(spec.svm, host_network=host_network)
                export.status.service_reloaded = True
                self._persist(
                    export,
                    "ganesha reloaded",
                    expected_create_owner=create_owner,
                    require_ready_volume=require_ready_volume,
                    require_ready_svm=require_ready_svm,
                )
            except CreateLeaseLostError:
                raise
            except PreconditionFailedError:
                self._rollback_svm_config(spec.svm, export_dir, host_network=host_network)
                raise
            except Exception as e:
                return self._fail_create(
                    export,
                    f"Reload failed: {e}",
                    export_dir,
                    host_network=host_network,
                    create_owner=create_owner,
                    previous_ready_export=previous_ready_export,
                )

            export.status.phase = Phase.READY
            expected_owner = create_owner
            clear_create_lease(export.status)
            export.status.message = ""
            export.status.last_reconciled = datetime.now(timezone.utc)
            self._persist(
                export,
                "Export ready",
                expected_create_owner=expected_owner,
                require_ready_volume=require_ready_volume,
                require_ready_svm=require_ready_svm,
            )
        return export

    def _fail_create(
        self,
        export: Export,
        message: str,
        export_dir: str,
        *,
        host_network: bool,
        create_owner: Optional[str],
        previous_ready_export: Optional[Export] = None,
    ) -> Export:
        export.status.phase = Phase.FAILED
        clear_create_lease(export.status)
        export.status.message = message

        if previous_ready_export is not None:
            self._persist(
                previous_ready_export,
                f"{message}; kept previous ready export",
                expected_create_owner=create_owner,
            )
        else:
            self._persist(export, message, expected_create_owner=create_owner)

        self._rollback_svm_config(export.spec.svm, export_dir, host_network=host_network)
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
            self._persist_conn(conn, export, "export delete reserved")

        with self._svm_config_lock(spec.svm):
            config_entries, bind_addr, host_network = self._config_snapshot_for_svm(spec.svm, export_dir)
            try:
                self.adapters.ganesha.render_config(
                    spec.svm,
                    config_entries,
                    bind_addr=bind_addr,
                    host_network=host_network,
                )
                self.adapters.ganesha.reload(spec.svm, host_network=host_network)
                with self.db.transaction(immediate=True) as conn:
                    self.db._delete_export_conn(conn, spec.svm, spec.volume, spec.client)
                    self.db._log_operation_conn(conn, "Export", export.metadata.id, "delete", "completed")
            except Exception as e:
                self._rollback_svm_config(
                    spec.svm,
                    export_dir,
                    host_network=host_network,
                    include_transient_key=(spec.svm, spec.volume, spec.client),
                )
                export.status.phase = Phase.FAILED
                export.status.message = f"Delete failed: {e}"
                self._persist(export, export.status.message)
        return export

    def _persist(
        self,
        export: Export,
        detail: str,
        *,
        expected_create_owner: Optional[str] = None,
        require_ready_volume: bool = False,
        require_ready_svm: bool = False,
    ) -> None:
        with self.db.transaction(immediate=True) as conn:
            self._persist_conn(
                conn,
                export,
                detail,
                expected_create_owner=expected_create_owner,
                require_ready_volume=require_ready_volume,
                require_ready_svm=require_ready_svm,
            )

    def _persist_conn(
        self,
        conn,
        export: Export,
        detail: str,
        *,
        expected_create_owner: Optional[str] = None,
        require_ready_volume: bool = False,
        require_ready_svm: bool = False,
        allow_missing_create_owner: bool = False,
    ) -> None:
        if not self.db._upsert_export_conn(
            conn,
            export,
            expected_create_owner=expected_create_owner,
            require_ready_volume=require_ready_volume,
            require_ready_svm=require_ready_svm,
            allow_missing_create_owner=allow_missing_create_owner,
        ):
            raise CreateLeaseLostError("Export", f"{export.spec.svm}/{export.spec.volume}/{export.spec.client}")
        self.db._log_operation_conn(
            conn, "Export", export.metadata.id, "reconcile", export.status.phase.value, detail
        )

    def _config_entries_for_svm(
        self,
        conn,
        svm_name: str,
        export_dir: str,
        *,
        include_transient_key: Optional[tuple[str, str, str]] = None,
    ) -> list[dict]:
        records = self.db._list_exports_conn(conn, svm=svm_name, limit=_all_rows_limit())
        return _records_to_config_entries(records, export_dir, include_transient_key=include_transient_key)

    def sync_svm_config(self, svm_name: str) -> str:
        """Render and reload one SVM config from DB-backed exports."""
        export_dir = self._cfg.get("export_dir", "/exports")
        with self._svm_config_lock(svm_name):
            config_entries, bind_addr, host_network = self._config_snapshot_for_svm(svm_name, export_dir)
            path = self.adapters.ganesha.render_config(
                svm_name,
                config_entries,
                bind_addr=bind_addr,
                host_network=host_network,
            )
            self.adapters.ganesha.reload(svm_name, host_network=host_network)
            return path

    def _config_snapshot_for_svm(
        self,
        svm_name: str,
        export_dir: str,
        *,
        include_transient_key: Optional[tuple[str, str, str]] = None,
    ) -> tuple[list[dict], Optional[str], bool]:
        with self.db.transaction() as conn:
            config_entries = self._config_entries_for_svm(
                conn,
                svm_name,
                export_dir,
                include_transient_key=include_transient_key,
            )
            bind_addr, host_network = self._ganesha_network_for_svm(conn, svm_name)
        return config_entries, bind_addr, host_network

    def _rollback_svm_config(
        self,
        svm_name: str,
        export_dir: str,
        *,
        host_network: bool,
        include_transient_key: Optional[tuple[str, str, str]] = None,
    ) -> None:
        """Best-effort restore of the active config to DB-ready exports."""
        try:
            config_entries, bind_addr, _ = self._config_snapshot_for_svm(
                svm_name,
                export_dir,
                include_transient_key=include_transient_key,
            )
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
            bind_addr, _prefix = validate_svm_ip_cidr(ip_cidr)
        except ValueError:
            bind_addr = None
        return bind_addr, spec.get("vlan_id") is None

    @contextmanager
    def _svm_config_lock(self, svm_name: str):
        """Serialize Ganesha config writes for one SVM without holding SQLite locks."""
        db_path = Path(str(getattr(self.db, "_db_path", "/var/lib/arca-storage/state.db")))
        lock_dir = db_path.parent / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"ganesha-{svm_name}.lock"
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

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
    include_transient_key: Optional[tuple[str, str, str]] = None,
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
        is_current_transient = (
            key == include_transient_key
            and phase in {Phase.CREATING.value, Phase.DELETING.value}
        )
        if phase != Phase.READY.value and not is_current_transient:
            continue

        try:
            path = _record_export_path(spec, status, export_dir)
            pseudo = _record_export_pseudo(spec, status, path)
        except (TypeError, ValueError) as e:
            logger.warning(
                "Skipping export %s/%s/%s with unsafe path data: %s",
                spec.get("svm"),
                spec.get("volume"),
                spec.get("client"),
                e,
            )
            continue

        entry = {
            "export_id": int(export_id),
            "path": path,
            "pseudo": pseudo,
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


def _can_resume_create_record(record: dict, requested_export: Export) -> bool:
    status = record.get("status", {})
    phase = status.get("phase")
    if ExportSpec.model_validate(record["spec"]) != requested_export.spec:
        return False
    if phase in ACTIVE_CREATE_PHASES:
        requested_owner = requested_export.status.create_owner
        if not requested_owner:
            return False
        return status.get("create_owner") == requested_owner
    if phase != Phase.FAILED.value:
        return False
    if str(status.get("message") or "").startswith("Delete failed:"):
        return False
    return not status.get("ganesha_configured", False) or not status.get("service_reloaded", False)


def _requires_ready_volume(export: Export) -> bool:
    return not (export.spec.owner == "csi" and export.spec.volume == "__csi_root__")


def _record_export_path(spec: dict, status: dict, export_dir: str) -> str:
    raw_path = status.get("path") or spec.get("path")
    if raw_path:
        return _normalize_absolute_export_path(raw_path, "export path")
    return _export_path(ExportSpec.model_validate(spec), export_dir)


def _record_export_pseudo(spec: dict, status: dict, path: str) -> str:
    return _normalize_absolute_export_path(status.get("pseudo") or spec.get("pseudo") or path, "export pseudo")


def _export_path(spec: ExportSpec, export_dir: str) -> str:
    if spec.path:
        return _normalize_absolute_export_path(spec.path, "export path")
    base = _normalize_absolute_export_path(export_dir, "export_dir")
    return _normalize_absolute_export_path(f"{base}/{spec.svm}/{spec.volume}", "export path")


def _export_pseudo(spec: ExportSpec, path: str) -> str:
    return _normalize_absolute_export_path(spec.pseudo or path, "export pseudo")


def _normalize_absolute_export_path(value: object, field_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} cannot be empty")
    if not raw.startswith("/"):
        raise ValueError(f"{field_name} must be an absolute POSIX path")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError(f"{field_name} must not contain control characters")

    parts = [part for part in raw.split("/") if part]
    if not parts:
        raise ValueError(f"{field_name} must not be the filesystem root")
    if any(part in {".", ".."} for part in parts):
        raise ValueError(f"{field_name} must not contain relative path segments")
    return "/" + "/".join(parts)


def _meta_from_record(record: dict) -> ResourceMeta:
    return resource_meta_from_record(record)


def _export_from_record(record: dict) -> Export:
    return Export(
        metadata=_meta_from_record(record),
        spec=ExportSpec.model_validate(record["spec"]),
        status=ExportStatus.model_validate(record["status"]),
    )


def _all_rows_limit() -> int:
    return 1_000_000
