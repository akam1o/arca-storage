"""
SQLite-backed state store for Arca Storage.

Replaces the JSON-file state store with ACID transactions, WAL mode,
and proper locking. No external daemon required — SQLite ships with Python.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Interface
from pathlib import Path
from typing import Any, Callable, Generator, Optional, Union

from arca_storage.create_resume import ACTIVE_CREATE_PHASES, create_lease_expired, lease_expiration
from arca_storage.errors import AlreadyExistsError, ConflictError, NotFoundError, PreconditionFailedError
from arca_storage.models.base import Phase


_SCHEMA_VERSION = 2
_SNAPSHOT_CLEANUP_RESERVATION_DURATION = timedelta(minutes=5)
_CSI_ROOT_EXPORT_VOLUME = "__csi_root__"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS svms (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    spec        TEXT NOT NULL,
    status      TEXT NOT NULL,
    generation  INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS volumes (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    svm         TEXT NOT NULL,
    spec        TEXT NOT NULL,
    status      TEXT NOT NULL,
    generation  INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(svm, name)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    svm         TEXT NOT NULL,
    volume      TEXT NOT NULL,
    spec        TEXT NOT NULL,
    status      TEXT NOT NULL,
    generation  INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(svm, volume, name)
);

CREATE TABLE IF NOT EXISTS snapshot_cleanup_reservations (
    svm         TEXT NOT NULL,
    volume      TEXT NOT NULL,
    name        TEXT NOT NULL,
    owner       TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY(svm, volume, name)
);

CREATE TABLE IF NOT EXISTS exports (
    id          TEXT PRIMARY KEY,
    svm         TEXT NOT NULL,
    volume      TEXT NOT NULL,
    client      TEXT NOT NULL,
    spec        TEXT NOT NULL,
    status      TEXT NOT NULL,
    generation  INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(svm, volume, client)
);

CREATE TABLE IF NOT EXISTS operation_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    operation   TEXT NOT NULL,
    phase       TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
"""


_REQUIRED_COLUMNS = {
    "schema_version": {"version"},
    "svms": {"id", "name", "spec", "status", "generation", "created_at", "updated_at"},
    "volumes": {"id", "name", "svm", "spec", "status", "generation", "created_at", "updated_at"},
    "snapshots": {"id", "name", "svm", "volume", "spec", "status", "generation", "created_at", "updated_at"},
    "snapshot_cleanup_reservations": {"svm", "volume", "name", "owner", "expires_at", "created_at"},
    "exports": {"id", "svm", "volume", "client", "spec", "status", "generation", "created_at", "updated_at"},
    "operation_log": {"id", "resource_type", "resource_id", "operation", "phase", "detail", "created_at"},
}


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshot_cleanup_reservations (
            svm         TEXT NOT NULL,
            volume      TEXT NOT NULL,
            name        TEXT NOT NULL,
            owner       TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            PRIMARY KEY(svm, volume, name)
        )"""
    )


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_to_v2,
}


def _validate_schema(conn: sqlite3.Connection) -> None:
    for table, required_columns in _REQUIRED_COLUMNS.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        actual_columns = {str(row["name"]) for row in rows}
        missing = sorted(required_columns - actual_columns)
        if missing:
            raise RuntimeError(f"State DB schema for table '{table}' is missing columns: {', '.join(missing)}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _svm_network_key(spec: dict[str, Any]) -> Optional[tuple[Optional[int], str]]:
    ip_cidr = str(spec.get("ip_cidr") or "")
    if not ip_cidr:
        return None
    try:
        vip = str(IPv4Interface(ip_cidr).ip)
    except Exception:
        vip = ip_cidr.split("/", 1)[0]
    if not vip:
        return None
    raw_vlan = spec.get("vlan_id")
    vlan_id = int(raw_vlan) if raw_vlan is not None else None
    return vlan_id, vip


def encode_cursor(values: list[str]) -> str:
    payload = json.dumps(values, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Optional[str], expected_parts: int) -> Optional[list[str]]:
    if not cursor:
        return None

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        values = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ValueError("Invalid pagination cursor") from e

    if (
        not isinstance(values, list)
        or len(values) != expected_parts
        or not all(isinstance(value, str) for value in values)
    ):
        raise ValueError("Invalid pagination cursor")
    return values


class StateDB:
    """Thread-safe SQLite state store with WAL journaling."""

    def __init__(self, db_path: Union[Path, str]) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._connections: dict[int, sqlite3.Connection] = {}
        self._connections_lock = threading.Lock()
        # Initialise schema on first connection
        try:
            with self.transaction() as conn:
                conn.executescript(_SCHEMA_SQL)
                cur = conn.execute("SELECT version FROM schema_version")
                row = cur.fetchone()
                if row is None:
                    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
                else:
                    current_version = int(row["version"])
                    if current_version > _SCHEMA_VERSION:
                        raise RuntimeError(
                            f"State DB schema version {current_version} is newer than supported version {_SCHEMA_VERSION}"
                        )
                    for version in range(current_version + 1, _SCHEMA_VERSION + 1):
                        migration = _MIGRATIONS.get(version)
                        if migration is None:
                            raise RuntimeError(f"No State DB migration registered for version {version}")
                        migration(conn)
                        conn.execute("UPDATE schema_version SET version = ?", (version,))
                _validate_schema(conn)
        except Exception:
            self.close()
            raise

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            with self._connections_lock:
                if id(conn) in self._connections:
                    return conn

        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        self._local.conn = conn
        with self._connections_lock:
            self._connections[id(conn)] = conn
        return conn

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Generator[sqlite3.Connection, None, None]:
        conn = self._conn()
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ---- SVM operations ----

    def insert_svm(self, svm: Any) -> None:
        """Insert a new SVM record without overwriting an existing one."""
        now = _now_iso()
        try:
            with self.transaction(immediate=True) as conn:
                self._raise_svm_network_conflict_conn(
                    conn,
                    svm.spec.model_dump(mode="json"),
                    exclude_name=svm.spec.name,
                )
                conn.execute(
                    """INSERT INTO svms (id, name, spec, status, generation, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        svm.metadata.id,
                        svm.spec.name,
                        svm.spec.model_dump_json(),
                        svm.status.model_dump_json(),
                        svm.metadata.generation,
                        svm.metadata.created_at.isoformat(),
                        now,
                    ),
                )
        except sqlite3.IntegrityError as e:
            raise AlreadyExistsError("SVM", svm.spec.name) from e

    def upsert_svm(self, svm: Any, *, expected_create_owner: Optional[str] = None) -> bool:
        """Insert or update an SVM record."""
        now = _now_iso()
        with self.transaction(immediate=True) as conn:
            if not self._create_owner_matches_conn(conn, "svms", {"name": svm.spec.name}, expected_create_owner):
                return False
            self._upsert_svm_conn(conn, svm, now=now)
            return True

    def _upsert_svm_conn(self, conn: sqlite3.Connection, svm: Any, *, now: Optional[str] = None) -> None:
        now = now or _now_iso()
        self._raise_svm_network_conflict_conn(
            conn,
            svm.spec.model_dump(mode="json"),
            exclude_name=svm.spec.name,
        )
        conn.execute(
            """INSERT INTO svms (id, name, spec, status, generation, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   spec=excluded.spec,
                   status=excluded.status,
                   generation=excluded.generation,
                   updated_at=excluded.updated_at
            """,
            (
                svm.metadata.id,
                svm.spec.name,
                svm.spec.model_dump_json(),
                svm.status.model_dump_json(),
                svm.metadata.generation,
                svm.metadata.created_at.isoformat(),
                now,
            ),
        )

    def get_svm(self, name: str) -> Optional[dict[str, Any]]:
        conn = self._conn()
        return self._get_svm_conn(conn, name)

    def acquire_svm_create_lease(
        self,
        name: str,
        owner: str,
        *,
        expected_spec: Optional[dict[str, Any]] = None,
        allow_failed: bool = False,
    ) -> Optional[dict[str, Any]]:
        return self._acquire_create_lease(
            "svms",
            {"name": name},
            owner,
            expected_spec=expected_spec,
            allow_failed=allow_failed,
        )

    def refresh_svm_create_lease(self, name: str, owner: str) -> bool:
        return self._refresh_create_lease("svms", {"name": name}, owner)

    def list_svms(
        self,
        name: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        conn = self._conn()
        cursor_values = _decode_cursor(cursor, 1)
        if name:
            if cursor_values:
                cur = conn.execute(
                    "SELECT * FROM svms WHERE name = ? AND name > ? ORDER BY name LIMIT ?",
                    (name, cursor_values[0], limit),
                )
            else:
                cur = conn.execute("SELECT * FROM svms WHERE name = ? ORDER BY name LIMIT ?", (name, limit))
        elif cursor_values:
            cur = conn.execute(
                "SELECT * FROM svms WHERE name > ? ORDER BY name LIMIT ?",
                (cursor_values[0], limit),
            )
        else:
            cur = conn.execute("SELECT * FROM svms ORDER BY name LIMIT ?", (limit,))
        return [self._row_to_resource(row) for row in cur.fetchall()]

    def delete_svm(self, name: str) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM svms WHERE name = ?", (name,))
            return cur.rowcount > 0

    def reserve_svm_delete(
        self,
        name: str,
        *,
        force: bool = False,
        delete_volumes: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Atomically mark an SVM deleting after validating dependent resources."""
        cascade_volumes = delete_volumes or force
        with self.transaction(immediate=True) as conn:
            record = self._get_svm_conn(conn, name)
            if record is None:
                return None

            volumes = self._list_volumes_conn(conn, svm=name, limit=1_000_000)
            resizing_volumes = [volume for volume in volumes if self._resize_lease_active(volume)]
            if resizing_volumes:
                raise ConflictError(
                    f"SVM '{name}' has volumes being resized; retry after resize completes",
                    {
                        "resource": "SVM",
                        "name": name,
                        "volume_count": len(resizing_volumes),
                        "volumes": [self._volume_ref(volume) for volume in resizing_volumes],
                    },
                )
            if volumes and not cascade_volumes:
                raise PreconditionFailedError(
                    f"SVM '{name}' has volumes; delete volumes first or retry with delete_volumes",
                    {
                        "resource": "SVM",
                        "name": name,
                        "volume_count": len(volumes),
                        "volumes": [self._volume_ref(volume) for volume in volumes],
                    },
                )

            snapshots = self._list_snapshots_conn(conn, svm=name, limit=1_000_000)
            if snapshots and not force:
                raise PreconditionFailedError(
                    f"SVM '{name}' has snapshots; delete snapshots first or retry with force",
                    {
                        "resource": "SVM",
                        "name": name,
                        "snapshot_count": len(snapshots),
                        "snapshots": [self._snapshot_ref(snapshot) for snapshot in snapshots],
                    },
                )
            active_clone_snapshots = [
                self._snapshot_ref(snapshot) for snapshot in snapshots if self._active_snapshot_clone_leases(snapshot)
            ]
            if active_clone_snapshots:
                raise ConflictError(
                    f"SVM '{name}' has snapshots that are being cloned; retry after clone completes",
                    {
                        "resource": "SVM",
                        "name": name,
                        "snapshots": active_clone_snapshots,
                    },
                )

            exports = self._blocking_exports_for_svm_delete(conn, name, volumes, force=force)
            if exports:
                raise PreconditionFailedError(
                    f"SVM '{name}' has exports; delete exports first or retry with force",
                    {
                        "resource": "SVM",
                        "name": name,
                        "export_count": len(exports),
                        "exports": [self._export_ref(export) for export in exports],
                    },
                )

            status = dict(record["status"])
            status["phase"] = Phase.DELETING.value
            status["message"] = ""
            status["create_owner"] = None
            status["create_lease_expires_at"] = None
            self._update_status_by_key_conn(conn, "svms", {"name": name}, status)
            record["status"] = status
            return record

    # ---- Volume operations ----

    def insert_volume(self, volume: Any, *, require_ready_svm: bool = False) -> None:
        """Insert a new volume record without overwriting an existing one."""
        now = _now_iso()
        try:
            with self.transaction(immediate=True) as conn:
                if require_ready_svm:
                    self._require_ready_svm_conn(conn, volume.spec.svm)
                conn.execute(
                    """INSERT INTO volumes (id, name, svm, spec, status, generation, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        volume.metadata.id,
                        volume.spec.name,
                        volume.spec.svm,
                        volume.spec.model_dump_json(),
                        volume.status.model_dump_json(),
                        volume.metadata.generation,
                        volume.metadata.created_at.isoformat(),
                        now,
                    ),
                )
        except sqlite3.IntegrityError as e:
            raise AlreadyExistsError("Volume", f"{volume.spec.svm}/{volume.spec.name}") from e

    def upsert_volume(self, volume: Any, *, expected_create_owner: Optional[str] = None) -> bool:
        now = _now_iso()
        key = {"svm": volume.spec.svm, "name": volume.spec.name}
        with self.transaction(immediate=expected_create_owner is not None) as conn:
            if not self._create_owner_matches_conn(conn, "volumes", key, expected_create_owner):
                return False
            self._upsert_volume_conn(conn, volume, now=now)
            return True

    def _upsert_volume_conn(self, conn: sqlite3.Connection, volume: Any, *, now: Optional[str] = None) -> None:
        now = now or _now_iso()
        conn.execute(
            """INSERT INTO volumes (id, name, svm, spec, status, generation, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(svm, name) DO UPDATE SET
                   spec=excluded.spec,
                   status=excluded.status,
                   generation=excluded.generation,
                   updated_at=excluded.updated_at
            """,
            (
                volume.metadata.id,
                volume.spec.name,
                volume.spec.svm,
                volume.spec.model_dump_json(),
                volume.status.model_dump_json(),
                volume.metadata.generation,
                volume.metadata.created_at.isoformat(),
                now,
            ),
        )

    def update_ready_volume(self, volume: Any) -> bool:
        """Update an existing READY volume without inserting a missing row."""
        now = _now_iso()
        with self.transaction(immediate=True) as conn:
            record = self._get_volume_conn(conn, volume.spec.svm, volume.spec.name)
            if record is None:
                return False
            if str(record.get("status", {}).get("phase") or "") != Phase.READY.value:
                return False
            if self._resize_lease_active(record):
                return False
            cur = conn.execute(
                """UPDATE volumes
                   SET spec = ?, status = ?, generation = ?, updated_at = ?
                   WHERE svm = ? AND name = ?
                """,
                (
                    volume.spec.model_dump_json(),
                    volume.status.model_dump_json(),
                    volume.metadata.generation,
                    now,
                    volume.spec.svm,
                    volume.spec.name,
                ),
            )
            return cur.rowcount > 0

    def reserve_volume_resize(
        self,
        svm: str,
        name: str,
        owner: str,
        target_size_gib: int,
    ) -> Optional[dict[str, Any]]:
        """Atomically reserve a READY volume for resize."""
        with self.transaction(immediate=True) as conn:
            if self._get_volume_conn(conn, svm, name) is None:
                return None
            self._require_ready_svm_conn(conn, svm)
            record = self._require_ready_volume_conn(conn, svm, name)
            if self._resize_lease_active(record):
                status = record.get("status", {})
                raise ConflictError(
                    f"Volume '{svm}/{name}' is already being resized",
                    {
                        "resource": "Volume",
                        "name": f"{svm}/{name}",
                        "resize_target_size_gib": status.get("resize_target_size_gib"),
                    },
                )

            current_size = int(record.get("spec", {}).get("size_gib") or 0)
            if target_size_gib < current_size:
                raise PreconditionFailedError(
                    f"Volume '{svm}/{name}' cannot be shrunk",
                    {
                        "resource": "Volume",
                        "name": f"{svm}/{name}",
                        "current_size_gib": current_size,
                        "requested_size_gib": target_size_gib,
                    },
                )
            if target_size_gib == current_size:
                return record

            status = dict(record["status"])
            status["resize_owner"] = owner
            status["resize_lease_expires_at"] = lease_expiration().isoformat()
            status["resize_target_size_gib"] = target_size_gib
            self._update_status_by_key_conn(conn, "volumes", {"svm": svm, "name": name}, status)
            record["status"] = status
            return record

    def refresh_volume_resize_lease(self, svm: str, name: str, owner: str) -> bool:
        with self.transaction(immediate=True) as conn:
            self._require_ready_svm_conn(conn, svm)
            record = self._get_volume_conn(conn, svm, name)
            if record is None:
                return False
            status = dict(record["status"])
            if str(status.get("phase") or "") != Phase.READY.value:
                return False
            if status.get("resize_owner") != owner:
                return False
            status["resize_lease_expires_at"] = lease_expiration().isoformat()
            self._update_status_by_key_conn(conn, "volumes", {"svm": svm, "name": name}, status)
            return True

    def complete_volume_resize(self, volume: Any, owner: str) -> bool:
        now = _now_iso()
        with self.transaction(immediate=True) as conn:
            self._require_ready_svm_conn(conn, volume.spec.svm)
            record = self._get_volume_conn(conn, volume.spec.svm, volume.spec.name)
            if record is None:
                return False
            current_status = record["status"]
            if str(current_status.get("phase") or "") != Phase.READY.value:
                return False
            if current_status.get("resize_owner") != owner:
                return False
            if current_status.get("resize_target_size_gib") != volume.spec.size_gib:
                return False

            status = json.loads(volume.status.model_dump_json())
            self._clear_resize_lease(status)
            cur = conn.execute(
                """UPDATE volumes
                   SET spec = ?, status = ?, generation = ?, updated_at = ?
                   WHERE svm = ? AND name = ?
                """,
                (
                    volume.spec.model_dump_json(),
                    json.dumps(status),
                    volume.metadata.generation,
                    now,
                    volume.spec.svm,
                    volume.spec.name,
                ),
            )
            return cur.rowcount > 0

    def release_volume_resize(self, svm: str, name: str, owner: str) -> None:
        with self.transaction(immediate=True) as conn:
            record = self._get_volume_conn(conn, svm, name)
            if record is None:
                return
            status = dict(record["status"])
            if status.get("resize_owner") != owner:
                return
            self._clear_resize_lease(status)
            self._update_status_by_key_conn(conn, "volumes", {"svm": svm, "name": name}, status)

    def set_volume_qos(self, svm: str, name: str, qos: Optional[dict[str, Any]]) -> bool:
        """Persist or clear QoS settings in the volume status."""
        with self.transaction(immediate=True) as conn:
            record = self._get_volume_conn(conn, svm, name)
            if record is None:
                return False
            status = dict(record["status"])
            if qos:
                status["qos"] = dict(qos)
            else:
                status.pop("qos", None)
            self._update_status_by_key_conn(conn, "volumes", {"svm": svm, "name": name}, status)
            return True

    def get_volume(self, svm: str, name: str) -> Optional[dict[str, Any]]:
        conn = self._conn()
        return self._get_volume_conn(conn, svm, name)

    def acquire_volume_create_lease(
        self,
        svm: str,
        name: str,
        owner: str,
        *,
        expected_spec: Optional[dict[str, Any]] = None,
        allow_failed: bool = False,
        require_ready_svm: bool = False,
    ) -> Optional[dict[str, Any]]:
        return self._acquire_create_lease(
            "volumes",
            {"svm": svm, "name": name},
            owner,
            expected_spec=expected_spec,
            allow_failed=allow_failed,
            precondition=(lambda conn: self._require_ready_svm_conn(conn, svm)) if require_ready_svm else None,
        )

    def refresh_volume_create_lease(
        self,
        svm: str,
        name: str,
        owner: str,
        *,
        require_ready_svm: bool = False,
    ) -> bool:
        return self._refresh_create_lease(
            "volumes",
            {"svm": svm, "name": name},
            owner,
            precondition=(lambda conn: self._require_ready_svm_conn(conn, svm)) if require_ready_svm else None,
        )

    def list_volumes(
        self,
        svm: Optional[str] = None,
        name: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        conn = self._conn()
        return self._list_volumes_conn(conn, svm=svm, name=name, limit=limit, cursor=cursor)

    def _list_volumes_conn(
        self,
        conn: sqlite3.Connection,
        svm: Optional[str] = None,
        name: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM volumes WHERE 1=1"
        params: list[Any] = []
        if svm:
            sql += " AND svm = ?"
            params.append(svm)
        if name:
            sql += " AND name = ?"
            params.append(name)
        cursor_values = _decode_cursor(cursor, 2)
        if cursor_values:
            cursor_svm, cursor_name = cursor_values
            sql += " AND (svm > ? OR (svm = ? AND name > ?))"
            params.extend([cursor_svm, cursor_svm, cursor_name])
        sql += " ORDER BY svm, name LIMIT ?"
        params.append(limit)
        cur = conn.execute(sql, params)
        return [self._row_to_resource(row) for row in cur.fetchall()]

    def delete_volume(self, svm: str, name: str) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM volumes WHERE svm = ? AND name = ?", (svm, name))
            return cur.rowcount > 0

    def reserve_volume_delete(self, svm: str, name: str, *, force: bool = False) -> Optional[dict[str, Any]]:
        """Atomically mark a volume deleting after validating snapshot preconditions."""
        with self.transaction(immediate=True) as conn:
            record = self._get_volume_conn(conn, svm, name)
            if record is None:
                return None
            if self._resize_lease_active(record):
                status = record.get("status", {})
                raise ConflictError(
                    f"Volume '{svm}/{name}' is being resized; retry after resize completes",
                    {
                        "resource": "Volume",
                        "name": f"{svm}/{name}",
                        "resize_target_size_gib": status.get("resize_target_size_gib"),
                    },
                )
            snapshots = self._list_snapshots_conn(conn, svm=svm, volume=name, limit=1_000_000)
            active_clone_snapshots = [
                self._snapshot_ref(snapshot) for snapshot in snapshots if self._active_snapshot_clone_leases(snapshot)
            ]
            if active_clone_snapshots:
                raise ConflictError(
                    f"Volume '{svm}/{name}' has snapshots that are being cloned; retry after clone completes",
                    {
                        "resource": "Volume",
                        "name": f"{svm}/{name}",
                        "snapshots": active_clone_snapshots,
                    },
                )
            if snapshots and not force:
                raise PreconditionFailedError(
                    f"Volume '{svm}/{name}' has snapshots; delete snapshots first or retry with force",
                    {
                        "resource": "Volume",
                        "name": f"{svm}/{name}",
                        "snapshot_count": len(snapshots),
                        "snapshots": [self._snapshot_ref(snapshot) for snapshot in snapshots],
                    },
                )

            status = dict(record["status"])
            status["phase"] = Phase.DELETING.value
            status["message"] = ""
            status["create_owner"] = None
            status["create_lease_expires_at"] = None
            self._update_status_by_key_conn(conn, "volumes", {"svm": svm, "name": name}, status)
            record["status"] = status
            return record

    # ---- Snapshot operations ----

    def insert_snapshot(
        self,
        snapshot: Any,
        *,
        require_ready_volume: bool = False,
        require_ready_svm: bool = False,
    ) -> None:
        """Insert a new snapshot record without overwriting an existing one."""
        now = _now_iso()
        try:
            with self.transaction(immediate=True) as conn:
                if require_ready_volume:
                    self._require_ready_volume_conn(
                        conn,
                        snapshot.spec.svm,
                        snapshot.spec.volume,
                        require_ready_svm=require_ready_svm,
                    )
                elif require_ready_svm:
                    self._require_ready_svm_conn(conn, snapshot.spec.svm)
                if self._snapshot_cleanup_reserved_conn(conn, snapshot.spec.svm, snapshot.spec.volume, snapshot.spec.name):
                    raise AlreadyExistsError(
                        "Snapshot",
                        f"{snapshot.spec.svm}/{snapshot.spec.volume}/{snapshot.spec.name}",
                    )
                conn.execute(
                    """INSERT INTO snapshots (id, name, svm, volume, spec, status, generation, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.metadata.id,
                        snapshot.spec.name,
                        snapshot.spec.svm,
                        snapshot.spec.volume,
                        snapshot.spec.model_dump_json(),
                        snapshot.status.model_dump_json(),
                        snapshot.metadata.generation,
                        snapshot.metadata.created_at.isoformat(),
                        now,
                    ),
                )
        except sqlite3.IntegrityError as e:
            raise AlreadyExistsError(
                "Snapshot",
                f"{snapshot.spec.svm}/{snapshot.spec.volume}/{snapshot.spec.name}",
            ) from e

    def upsert_snapshot(
        self,
        snapshot: Any,
        *,
        expected_create_owner: Optional[str] = None,
        require_ready_volume: bool = False,
        require_ready_svm: bool = False,
    ) -> bool:
        now = _now_iso()
        key = {"svm": snapshot.spec.svm, "volume": snapshot.spec.volume, "name": snapshot.spec.name}
        with self.transaction(immediate=True) as conn:
            if not self._create_owner_matches_conn(conn, "snapshots", key, expected_create_owner):
                return False
            if require_ready_volume:
                self._require_ready_volume_conn(
                    conn,
                    snapshot.spec.svm,
                    snapshot.spec.volume,
                    require_ready_svm=require_ready_svm,
                )
            elif require_ready_svm:
                self._require_ready_svm_conn(conn, snapshot.spec.svm)
            if self._get_resource_by_key_conn(conn, "snapshots", key) is None and self._snapshot_cleanup_reserved_conn(
                conn,
                snapshot.spec.svm,
                snapshot.spec.volume,
                snapshot.spec.name,
            ):
                raise AlreadyExistsError(
                    "Snapshot",
                    f"{snapshot.spec.svm}/{snapshot.spec.volume}/{snapshot.spec.name}",
                )
            self._upsert_snapshot_conn(conn, snapshot, now=now)
            return True

    def _upsert_snapshot_conn(self, conn: sqlite3.Connection, snapshot: Any, *, now: Optional[str] = None) -> None:
        now = now or _now_iso()
        conn.execute(
            """INSERT INTO snapshots (id, name, svm, volume, spec, status, generation, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(svm, volume, name) DO UPDATE SET
                   spec=excluded.spec,
                   status=excluded.status,
                   generation=excluded.generation,
                   updated_at=excluded.updated_at
            """,
            (
                snapshot.metadata.id,
                snapshot.spec.name,
                snapshot.spec.svm,
                snapshot.spec.volume,
                snapshot.spec.model_dump_json(),
                snapshot.status.model_dump_json(),
                snapshot.metadata.generation,
                snapshot.metadata.created_at.isoformat(),
                now,
            ),
        )

    def acquire_snapshot_create_lease(
        self,
        svm: str,
        volume: str,
        name: str,
        owner: str,
        *,
        expected_spec: Optional[dict[str, Any]] = None,
        allow_failed: bool = False,
        require_ready_volume: bool = False,
        require_ready_svm: bool = False,
    ) -> Optional[dict[str, Any]]:
        return self._acquire_create_lease(
            "snapshots",
            {"svm": svm, "volume": volume, "name": name},
            owner,
            expected_spec=expected_spec,
            allow_failed=allow_failed,
            precondition=(
                (lambda conn: self._require_ready_volume_conn(conn, svm, volume, require_ready_svm=require_ready_svm))
                if require_ready_volume
                else (lambda conn: self._require_ready_svm_conn(conn, svm))
                if require_ready_svm
                else None
            ),
        )

    def refresh_snapshot_create_lease(
        self,
        svm: str,
        volume: str,
        name: str,
        owner: str,
        *,
        require_ready_volume: bool = False,
        require_ready_svm: bool = False,
    ) -> bool:
        return self._refresh_create_lease(
            "snapshots",
            {"svm": svm, "volume": volume, "name": name},
            owner,
            precondition=(
                (lambda conn: self._require_ready_volume_conn(conn, svm, volume, require_ready_svm=require_ready_svm))
                if require_ready_volume
                else (lambda conn: self._require_ready_svm_conn(conn, svm))
                if require_ready_svm
                else None
            ),
        )

    def list_snapshots(
        self,
        svm: Optional[str] = None,
        volume: Optional[str] = None,
        name: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        conn = self._conn()
        return self._list_snapshots_conn(conn, svm=svm, volume=volume, name=name, limit=limit, cursor=cursor)

    def _list_snapshots_conn(
        self,
        conn: sqlite3.Connection,
        svm: Optional[str] = None,
        volume: Optional[str] = None,
        name: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM snapshots WHERE 1=1"
        params: list[Any] = []
        if svm:
            sql += " AND svm = ?"
            params.append(svm)
        if volume:
            sql += " AND volume = ?"
            params.append(volume)
        if name:
            sql += " AND name = ?"
            params.append(name)
        cursor_values = _decode_cursor(cursor, 3)
        if cursor_values:
            cursor_svm, cursor_volume, cursor_name = cursor_values
            sql += (
                " AND (svm > ? OR (svm = ? AND volume > ?) "
                "OR (svm = ? AND volume = ? AND name > ?))"
            )
            params.extend([cursor_svm, cursor_svm, cursor_volume, cursor_svm, cursor_volume, cursor_name])
        sql += " ORDER BY svm, volume, name LIMIT ?"
        params.append(limit)
        cur = conn.execute(sql, params)
        return [self._row_to_resource(row) for row in cur.fetchall()]

    def delete_snapshot(self, svm: str, volume: str, name: str) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM snapshots WHERE svm = ? AND volume = ? AND name = ?",
                (svm, volume, name),
            )
            return cur.rowcount > 0

    def reserve_snapshot_clone(
        self,
        svm: str,
        volume: str,
        name: str,
        owner: str,
    ) -> Optional[dict[str, Any]]:
        """Reserve a READY snapshot so delete cannot remove it during clone."""
        key = {"svm": svm, "volume": volume, "name": name}
        with self.transaction(immediate=True) as conn:
            record = self._get_resource_by_key_conn(conn, "snapshots", key)
            if record is None:
                return None
            status = dict(record["status"])
            phase = status.get("phase")
            if phase != Phase.READY.value or not status.get("lv_created"):
                raise PreconditionFailedError(
                    f"Snapshot '{svm}/{volume}/{name}' is not ready",
                    {
                        "resource": "Snapshot",
                        "name": f"{svm}/{volume}/{name}",
                        "phase": phase,
                    },
                )
            leases = self._pruned_snapshot_clone_leases(status)
            leases[owner] = lease_expiration().isoformat()
            status["clone_leases"] = leases
            self._update_status_by_key_conn(conn, "snapshots", key, status)
            record["status"] = status
            return record

    def refresh_snapshot_clone_lease(self, svm: str, volume: str, name: str, owner: str) -> bool:
        key = {"svm": svm, "volume": volume, "name": name}
        with self.transaction(immediate=True) as conn:
            record = self._get_resource_by_key_conn(conn, "snapshots", key)
            if record is None:
                return False
            status = dict(record["status"])
            if status.get("phase") != Phase.READY.value or not status.get("lv_created"):
                return False
            leases = self._pruned_snapshot_clone_leases(status)
            if owner not in leases:
                return False
            leases[owner] = lease_expiration().isoformat()
            status["clone_leases"] = leases
            self._update_status_by_key_conn(conn, "snapshots", key, status)
            return True

    def release_snapshot_clone(self, svm: str, volume: str, name: str, owner: str) -> None:
        key = {"svm": svm, "volume": volume, "name": name}
        with self.transaction(immediate=True) as conn:
            record = self._get_resource_by_key_conn(conn, "snapshots", key)
            if record is None:
                return
            status = dict(record["status"])
            leases = self._pruned_snapshot_clone_leases(status)
            leases.pop(owner, None)
            if leases:
                status["clone_leases"] = leases
            else:
                status.pop("clone_leases", None)
            self._update_status_by_key_conn(conn, "snapshots", key, status)

    def reserve_snapshot_delete(
        self,
        svm: str,
        volume: str,
        name: str,
        *,
        force: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Atomically mark a snapshot deleting after validating clone preconditions."""
        key = {"svm": svm, "volume": volume, "name": name}
        with self.transaction(immediate=True) as conn:
            record = self._get_resource_by_key_conn(conn, "snapshots", key)
            if record is None:
                return None
            active_leases = self._active_snapshot_clone_leases(record)
            if active_leases:
                raise ConflictError(
                    f"Snapshot '{svm}/{volume}/{name}' is being cloned; retry after clone completes",
                    {
                        "resource": "Snapshot",
                        "name": f"{svm}/{volume}/{name}",
                        "clone_owners": sorted(active_leases),
                    },
                )
            status = dict(record["status"])
            status["phase"] = Phase.DELETING.value
            status["message"] = ""
            status["create_owner"] = None
            status["create_lease_expires_at"] = None
            status.pop("clone_leases", None)
            self._update_status_by_key_conn(conn, "snapshots", key, status)
            record["status"] = status
            return record

    def reserve_snapshot_cleanup(self, svm: str, volume: str, name: str, owner: str) -> bool:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_at = (now + _SNAPSHOT_CLEANUP_RESERVATION_DURATION).isoformat()
        key = {"svm": svm, "volume": volume, "name": name}
        with self.transaction(immediate=True) as conn:
            if self._get_resource_by_key_conn(conn, "snapshots", key) is not None:
                return False
            cur = conn.execute(
                """SELECT expires_at FROM snapshot_cleanup_reservations
                   WHERE svm = ? AND volume = ? AND name = ?
                """,
                (svm, volume, name),
            )
            reservation = cur.fetchone()
            if reservation is not None:
                if str(reservation["expires_at"]) > now_iso:
                    return False
                conn.execute(
                    """UPDATE snapshot_cleanup_reservations
                       SET owner = ?, expires_at = ?, created_at = ?
                       WHERE svm = ? AND volume = ? AND name = ?
                    """,
                    (owner, expires_at, now_iso, svm, volume, name),
                )
                return True
            try:
                conn.execute(
                    """INSERT INTO snapshot_cleanup_reservations (svm, volume, name, owner, expires_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (svm, volume, name, owner, expires_at, now_iso),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def release_snapshot_cleanup(self, svm: str, volume: str, name: str, owner: str) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute(
                """DELETE FROM snapshot_cleanup_reservations
                   WHERE svm = ? AND volume = ? AND name = ? AND owner = ?
                """,
                (svm, volume, name, owner),
            )

    def _snapshot_cleanup_reserved_conn(self, conn: sqlite3.Connection, svm: str, volume: str, name: str) -> bool:
        cur = conn.execute(
            """SELECT 1 FROM snapshot_cleanup_reservations
               WHERE svm = ? AND volume = ? AND name = ?
               LIMIT 1
            """,
            (svm, volume, name),
        )
        return cur.fetchone() is not None

    # ---- Export operations ----

    def upsert_export(
        self,
        export: Any,
        *,
        expected_create_owner: Optional[str] = None,
        require_ready_volume: bool = False,
        require_ready_svm: bool = False,
        allow_missing_create_owner: bool = True,
    ) -> bool:
        now = _now_iso()
        with self.transaction(immediate=expected_create_owner is not None) as conn:
            return self._upsert_export_conn(
                conn,
                export,
                now=now,
                expected_create_owner=expected_create_owner,
                require_ready_volume=require_ready_volume,
                require_ready_svm=require_ready_svm,
                allow_missing_create_owner=allow_missing_create_owner,
            )

    def _upsert_export_conn(
        self,
        conn: sqlite3.Connection,
        export: Any,
        *,
        now: Optional[str] = None,
        expected_create_owner: Optional[str] = None,
        require_ready_volume: bool = False,
        require_ready_svm: bool = False,
        allow_missing_create_owner: bool = True,
    ) -> bool:
        now = now or _now_iso()
        key = {"svm": export.spec.svm, "volume": export.spec.volume, "client": export.spec.client}
        if not self._create_owner_matches_conn(
            conn,
            "exports",
            key,
            expected_create_owner,
            allow_missing=allow_missing_create_owner,
        ):
            return False
        if require_ready_volume:
            self._require_ready_volume_conn(
                conn,
                export.spec.svm,
                export.spec.volume,
                require_ready_svm=require_ready_svm,
            )
        elif require_ready_svm:
            self._require_ready_svm_conn(conn, export.spec.svm)
        conn.execute(
            """INSERT INTO exports (id, svm, volume, client, spec, status, generation, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(svm, volume, client) DO UPDATE SET
                   spec=excluded.spec,
                   status=excluded.status,
                   generation=excluded.generation,
                   updated_at=excluded.updated_at
            """,
            (
                export.metadata.id,
                export.spec.svm,
                export.spec.volume,
                export.spec.client,
                export.spec.model_dump_json(),
                export.status.model_dump_json(),
                export.metadata.generation,
                export.metadata.created_at.isoformat(),
                now,
            ),
        )
        return True

    def list_exports(
        self,
        svm: Optional[str] = None,
        volume: Optional[str] = None,
        client: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        conn = self._conn()
        return self._list_exports_conn(conn, svm=svm, volume=volume, client=client, limit=limit, cursor=cursor)

    def get_export(self, svm: str, volume: str, client: str) -> Optional[dict[str, Any]]:
        conn = self._conn()
        return self._get_export_conn(conn, svm, volume, client)

    def acquire_export_create_lease(
        self,
        svm: str,
        volume: str,
        client: str,
        owner: str,
        *,
        expected_spec: Optional[dict[str, Any]] = None,
        allow_failed: bool = False,
        require_ready_svm: bool = False,
    ) -> Optional[dict[str, Any]]:
        return self._acquire_create_lease(
            "exports",
            {"svm": svm, "volume": volume, "client": client},
            owner,
            expected_spec=expected_spec,
            allow_failed=allow_failed,
            precondition=(lambda conn: self._require_ready_svm_conn(conn, svm)) if require_ready_svm else None,
        )

    def refresh_export_create_lease(
        self,
        svm: str,
        volume: str,
        client: str,
        owner: str,
        *,
        require_ready_svm: bool = False,
    ) -> bool:
        return self._refresh_create_lease(
            "exports",
            {"svm": svm, "volume": volume, "client": client},
            owner,
            precondition=(lambda conn: self._require_ready_svm_conn(conn, svm)) if require_ready_svm else None,
        )

    def _get_export_conn(
        self,
        conn: sqlite3.Connection,
        svm: str,
        volume: str,
        client: str,
    ) -> Optional[dict[str, Any]]:
        cur = conn.execute(
            "SELECT * FROM exports WHERE svm = ? AND volume = ? AND client = ?",
            (svm, volume, client),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_resource(row)

    def _list_exports_conn(
        self,
        conn: sqlite3.Connection,
        svm: Optional[str] = None,
        volume: Optional[str] = None,
        client: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM exports WHERE 1=1"
        params: list[Any] = []
        if svm:
            sql += " AND svm = ?"
            params.append(svm)
        if volume:
            sql += " AND volume = ?"
            params.append(volume)
        if client:
            sql += " AND client = ?"
            params.append(client)
        cursor_values = _decode_cursor(cursor, 3)
        if cursor_values:
            cursor_svm, cursor_volume, cursor_client = cursor_values
            sql += (
                " AND (svm > ? OR (svm = ? AND volume > ?) "
                "OR (svm = ? AND volume = ? AND client > ?))"
            )
            params.extend([cursor_svm, cursor_svm, cursor_volume, cursor_svm, cursor_volume, cursor_client])
        sql += " ORDER BY svm, volume, client LIMIT ?"
        params.append(limit)
        cur = conn.execute(sql, params)
        return [self._row_to_resource(row) for row in cur.fetchall()]

    def delete_export(self, svm: str, volume: str, client: str) -> bool:
        with self.transaction() as conn:
            return self._delete_export_conn(conn, svm, volume, client)

    def _delete_export_conn(self, conn: sqlite3.Connection, svm: str, volume: str, client: str) -> bool:
        cur = conn.execute(
            "DELETE FROM exports WHERE svm = ? AND volume = ? AND client = ?",
            (svm, volume, client),
        )
        return cur.rowcount > 0

    # ---- Operation log ----

    def log_operation(
        self,
        resource_type: str,
        resource_id: str,
        operation: str,
        phase: str,
        detail: str = "",
    ) -> None:
        with self.transaction() as conn:
            self._log_operation_conn(conn, resource_type, resource_id, operation, phase, detail)

    def _log_operation_conn(
        self,
        conn: sqlite3.Connection,
        resource_type: str,
        resource_id: str,
        operation: str,
        phase: str,
        detail: str = "",
    ) -> None:
        conn.execute(
            """INSERT INTO operation_log (resource_type, resource_id, operation, phase, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (resource_type, resource_id, operation, phase, detail, _now_iso()),
        )

    # ---- helpers ----

    def _acquire_create_lease(
        self,
        table: str,
        key: dict[str, str],
        owner: str,
        *,
        expected_spec: Optional[dict[str, Any]] = None,
        allow_failed: bool = False,
        precondition: Optional[Callable[[sqlite3.Connection], None]] = None,
    ) -> Optional[dict[str, Any]]:
        with self.transaction(immediate=True) as conn:
            if precondition is not None:
                precondition(conn)
            record = self._get_resource_by_key_conn(conn, table, key)
            if record is None:
                return None
            if expected_spec is not None and record.get("spec") != expected_spec:
                return None
            status = record["status"]
            phase = status.get("phase")
            if phase in ACTIVE_CREATE_PHASES:
                if not create_lease_expired(record):
                    return None
            elif phase != Phase.FAILED.value or not allow_failed:
                return None
            status["phase"] = Phase.CREATING.value
            status["create_owner"] = owner
            status["create_lease_expires_at"] = lease_expiration().isoformat()
            self._update_status_by_key_conn(conn, table, key, status)
            record["status"] = status
            return record

    def _create_owner_matches_conn(
        self,
        conn: sqlite3.Connection,
        table: str,
        key: dict[str, str],
        expected_owner: Optional[str],
        *,
        allow_missing: bool = False,
    ) -> bool:
        if expected_owner is None:
            return True
        record = self._get_resource_by_key_conn(conn, table, key)
        if record is None:
            return allow_missing
        return record["status"].get("create_owner") == expected_owner

    def _refresh_create_lease(
        self,
        table: str,
        key: dict[str, str],
        owner: str,
        *,
        precondition: Optional[Callable[[sqlite3.Connection], None]] = None,
    ) -> bool:
        with self.transaction(immediate=True) as conn:
            if precondition is not None:
                precondition(conn)
            record = self._get_resource_by_key_conn(conn, table, key)
            if record is None:
                return False
            status = record["status"]
            if status.get("phase") not in ACTIVE_CREATE_PHASES or status.get("create_owner") != owner:
                return False
            status["create_lease_expires_at"] = lease_expiration().isoformat()
            self._update_status_by_key_conn(conn, table, key, status)
            return True

    def _get_resource_by_key_conn(
        self,
        conn: sqlite3.Connection,
        table: str,
        key: dict[str, str],
    ) -> Optional[dict[str, Any]]:
        where = " AND ".join(f"{column} = ?" for column in key)
        cur = conn.execute(f"SELECT * FROM {table} WHERE {where}", tuple(key.values()))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_resource(row)

    def _get_svm_conn(self, conn: sqlite3.Connection, name: str) -> Optional[dict[str, Any]]:
        cur = conn.execute("SELECT * FROM svms WHERE name = ?", (name,))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_resource(row)

    def _get_volume_conn(self, conn: sqlite3.Connection, svm: str, name: str) -> Optional[dict[str, Any]]:
        cur = conn.execute("SELECT * FROM volumes WHERE svm = ? AND name = ?", (svm, name))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_resource(row)

    def _require_ready_svm_conn(self, conn: sqlite3.Connection, name: str) -> dict[str, Any]:
        record = self._get_svm_conn(conn, name)
        if record is None:
            raise NotFoundError("SVM", name)
        phase = str(record.get("status", {}).get("phase") or "")
        if phase != Phase.READY.value:
            raise PreconditionFailedError(
                f"SVM '{name}' is not ready",
                {
                    "resource": "SVM",
                    "name": name,
                    "phase": phase,
                },
            )
        return record

    def _require_ready_volume_conn(
        self,
        conn: sqlite3.Connection,
        svm: str,
        name: str,
        *,
        require_ready_svm: bool = False,
    ) -> dict[str, Any]:
        if require_ready_svm:
            self._require_ready_svm_conn(conn, svm)
        record = self._get_volume_conn(conn, svm, name)
        if record is None:
            raise NotFoundError("Volume", f"{svm}/{name}")
        phase = str(record.get("status", {}).get("phase") or "")
        if phase != Phase.READY.value:
            raise PreconditionFailedError(
                f"Volume '{svm}/{name}' is not ready",
                {
                    "resource": "Volume",
                    "name": f"{svm}/{name}",
                    "phase": phase,
                },
            )
        if self._resize_lease_active(record):
            status = record.get("status", {})
            raise ConflictError(
                f"Volume '{svm}/{name}' is being resized",
                {
                    "resource": "Volume",
                    "name": f"{svm}/{name}",
                    "resize_target_size_gib": status.get("resize_target_size_gib"),
                },
            )
        return record

    def _update_status_by_key_conn(
        self,
        conn: sqlite3.Connection,
        table: str,
        key: dict[str, str],
        status: dict[str, Any],
    ) -> None:
        where = " AND ".join(f"{column} = ?" for column in key)
        conn.execute(
            f"UPDATE {table} SET status = ?, updated_at = ? WHERE {where}",
            (json.dumps(status), _now_iso(), *key.values()),
        )

    def _raise_svm_network_conflict_conn(
        self,
        conn: sqlite3.Connection,
        spec: dict[str, Any],
        *,
        exclude_name: str,
    ) -> None:
        key = _svm_network_key(spec)
        if key is None:
            return
        vlan_id, vip = key
        cur = conn.execute("SELECT name, spec FROM svms")
        for row in cur.fetchall():
            existing_name = row["name"]
            if existing_name == exclude_name:
                continue
            existing_spec = json.loads(row["spec"])
            if _svm_network_key(existing_spec) == key:
                vlan_label = "host network" if vlan_id is None else f"VLAN {vlan_id}"
                raise ConflictError(
                    f"IP address {vip} is already in use on {vlan_label} by SVM '{existing_name}'",
                    {
                        "resource": "SVM",
                        "name": exclude_name,
                        "ip": vip,
                        "vlan_id": vlan_id,
                        "conflicting_svm": existing_name,
                    },
                )

    def _blocking_exports_for_svm_delete(
        self,
        conn: sqlite3.Connection,
        svm: str,
        volumes: list[dict[str, Any]],
        *,
        force: bool,
    ) -> list[dict[str, Any]]:
        if force:
            return []

        exports = self._list_exports_conn(conn, svm=svm, limit=1_000_000)
        volume_names = {str(volume.get("spec", {}).get("name") or "") for volume in volumes}
        if not volume_names:
            return exports

        blocking: list[dict[str, Any]] = []
        for export in exports:
            spec = export.get("spec", {})
            volume_name = spec.get("volume")
            if volume_name in volume_names:
                continue
            if spec.get("owner", "api") == "csi" and volume_name == _CSI_ROOT_EXPORT_VOLUME:
                continue
            blocking.append(export)
        return blocking

    @staticmethod
    def _resize_lease_active(record: dict[str, Any]) -> bool:
        status = record.get("status", {})
        if not status.get("resize_owner"):
            return False
        raw_expires_at = status.get("resize_lease_expires_at")
        if not raw_expires_at:
            return True
        try:
            expires_at = datetime.fromisoformat(str(raw_expires_at).replace("Z", "+00:00"))
        except ValueError:
            return True
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at.astimezone(timezone.utc) > datetime.now(timezone.utc)

    @classmethod
    def _active_snapshot_clone_leases(cls, record: dict[str, Any]) -> dict[str, str]:
        status = dict(record.get("status", {}))
        return cls._pruned_snapshot_clone_leases(status)

    @staticmethod
    def _pruned_snapshot_clone_leases(status: dict[str, Any]) -> dict[str, str]:
        raw_leases = status.get("clone_leases")
        if not isinstance(raw_leases, dict):
            return {}
        now = datetime.now(timezone.utc)
        active: dict[str, str] = {}
        for owner, raw_expires_at in raw_leases.items():
            if not owner:
                continue
            if not raw_expires_at:
                active[str(owner)] = ""
                continue
            try:
                expires_at = datetime.fromisoformat(str(raw_expires_at).replace("Z", "+00:00"))
            except ValueError:
                active[str(owner)] = str(raw_expires_at)
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at.astimezone(timezone.utc) > now:
                active[str(owner)] = str(raw_expires_at)
        return active

    @staticmethod
    def _clear_resize_lease(status: dict[str, Any]) -> None:
        status["resize_owner"] = None
        status["resize_lease_expires_at"] = None
        status["resize_target_size_gib"] = None

    @staticmethod
    def _volume_ref(volume: dict[str, Any]) -> str:
        spec = volume.get("spec", {})
        return f"{spec.get('svm')}/{spec.get('name')}"

    @staticmethod
    def _snapshot_ref(snapshot: dict[str, Any]) -> str:
        spec = snapshot.get("spec", {})
        return f"{spec.get('svm')}/{spec.get('volume')}/{spec.get('name')}"

    @staticmethod
    def _export_ref(export: dict[str, Any]) -> str:
        spec = export.get("spec", {})
        return f"{spec.get('svm')}/{spec.get('volume')}/{spec.get('client')}"

    @staticmethod
    def _row_to_resource(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["spec"] = json.loads(d["spec"])
        d["status"] = json.loads(d["status"])
        return d

    def close(self) -> None:
        with self._connections_lock:
            connections = list(self._connections.values())
            self._connections.clear()
        for conn in connections:
            conn.close()
        self._local.conn = None
