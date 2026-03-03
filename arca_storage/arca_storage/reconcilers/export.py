"""
Export Reconciler — drives NFS export resources from desired to actual state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from arca_storage.db import StateDB
from arca_storage.models.base import Phase
from arca_storage.models.export import Export
from arca_storage.reconcilers.adapters import Adapters

logger = logging.getLogger(__name__)


class ExportReconciler:
    def __init__(self, db: StateDB, adapters: Adapters, *, config: Optional[dict] = None) -> None:
        self.db = db
        self.adapters = adapters
        self._cfg = config or {}

    def reconcile(self, export: Export) -> Export:
        phase = export.status.phase
        if phase in (Phase.PENDING, Phase.CREATING):
            return self._reconcile_create(export)
        elif phase == Phase.DELETING:
            return self._reconcile_delete(export)
        elif phase in (Phase.READY, Phase.FAILED):
            return export
        return export

    def _reconcile_create(self, export: Export) -> Export:
        export.status.phase = Phase.CREATING
        spec = export.spec
        export_dir = self._cfg.get("export_dir", "/exports")

        # Step 1: update exports list and render config
        if not export.status.ganesha_configured:
            try:
                exports_list = self.adapters.ganesha.load_exports(spec.svm)
                export_id = max([e.get("export_id", 0) for e in exports_list], default=0) + 1
                entry = {
                    "export_id": export_id,
                    "path": f"{export_dir}/{spec.svm}/{spec.volume}",
                    "pseudo": f"{export_dir}/{spec.svm}/{spec.volume}",
                    "access": spec.access.upper(),
                    "squash": "Root_Squash" if spec.root_squash else "No_Root_Squash",
                    "sec": spec.sec,
                    "client": spec.client,
                }
                exports_list.append(entry)
                self.adapters.ganesha.save_exports(spec.svm, exports_list)
                self.adapters.ganesha.render_config(spec.svm, exports_list)

                export.status.export_id = export_id
                export.status.path = entry["path"]
                export.status.pseudo = entry["pseudo"]
                export.status.ganesha_configured = True
                self._persist(export, "ganesha config rendered")
            except Exception as e:
                export.status.phase = Phase.FAILED
                export.status.message = f"Config failed: {e}"
                self._persist(export, export.status.message)
                return export

        # Step 2: reload service
        if not export.status.service_reloaded:
            try:
                self.adapters.ganesha.reload(spec.svm)
                export.status.service_reloaded = True
                self._persist(export, "ganesha reloaded")
            except Exception as e:
                export.status.phase = Phase.FAILED
                export.status.message = f"Reload failed: {e}"
                self._persist(export, export.status.message)
                return export

        export.status.phase = Phase.READY
        export.status.message = ""
        export.status.last_reconciled = datetime.now(timezone.utc)
        self._persist(export, "Export ready")
        return export

    def _reconcile_delete(self, export: Export) -> Export:
        spec = export.spec
        export_dir = self._cfg.get("export_dir", "/exports")
        try:
            exports_list = self.adapters.ganesha.load_exports(spec.svm)
            path = f"{export_dir}/{spec.svm}/{spec.volume}"
            exports_list = [
                e for e in exports_list
                if not (e.get("path") == path and e.get("client") == spec.client)
            ]
            self.adapters.ganesha.save_exports(spec.svm, exports_list)
            self.adapters.ganesha.render_config(spec.svm, exports_list)
            self.adapters.ganesha.reload(spec.svm)
            self.db.delete_export(spec.svm, spec.volume, spec.client)
            self.db.log_operation("Export", export.metadata.id, "delete", "completed")
        except Exception as e:
            export.status.phase = Phase.FAILED
            export.status.message = f"Delete failed: {e}"
            self._persist(export, export.status.message)
        return export

    def _persist(self, export: Export, detail: str) -> None:
        self.db.upsert_export(export)
        self.db.log_operation(
            "Export", export.metadata.id, "reconcile", export.status.phase.value, detail
        )
