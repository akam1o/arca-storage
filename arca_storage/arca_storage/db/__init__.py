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
        with self.transaction() as conn:
            conn.executescript(_SCHEMA_SQL)
            cur = conn.execute("SELECT version FROM schema_version")
            row = cur.fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
            elif int(row["version"]) < _SCHEMA_VERSION:
                conn.execute("UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,))

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
        cur = conn.execute("SELECT * FROM svms WHERE name = ?", (name,))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_resource(row)

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

    # ---- Volume operations ----

    def insert_volume(self, volume: Any) -> None:
        """Insert a new volume record without overwriting an existing one."""
        now = _now_iso()
        try:
            with self.transaction(immediate=True) as conn:
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
    ) -> Optional[dict[str, Any]]:
        return self._acquire_create_lease(
            "volumes",
            {"svm": svm, "name": name},
            owner,
            expected_spec=expected_spec,
            allow_failed=allow_failed,
        )

    def refresh_volume_create_lease(self, svm: str, name: str, owner: str) -> bool:
        return self._refresh_create_lease("volumes", {"svm": svm, "name": name}, owner)

    def list_volumes(
        self,
        svm: Optional[str] = None,
        name: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        conn = self._conn()
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
            snapshots = self._list_snapshots_conn(conn, svm=svm, volume=name, limit=1_000_000)
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

    def insert_snapshot(self, snapshot: Any, *, require_ready_volume: bool = False) -> None:
        """Insert a new snapshot record without overwriting an existing one."""
        now = _now_iso()
        try:
            with self.transaction(immediate=True) as conn:
                if require_ready_volume:
                    self._require_ready_volume_conn(conn, snapshot.spec.svm, snapshot.spec.volume)
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
    ) -> bool:
        now = _now_iso()
        key = {"svm": snapshot.spec.svm, "volume": snapshot.spec.volume, "name": snapshot.spec.name}
        with self.transaction(immediate=expected_create_owner is not None) as conn:
            if not self._create_owner_matches_conn(conn, "snapshots", key, expected_create_owner):
                return False
            if require_ready_volume:
                self._require_ready_volume_conn(conn, snapshot.spec.svm, snapshot.spec.volume)
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
    ) -> Optional[dict[str, Any]]:
        return self._acquire_create_lease(
            "snapshots",
            {"svm": svm, "volume": volume, "name": name},
            owner,
            expected_spec=expected_spec,
            allow_failed=allow_failed,
            precondition=(
                (lambda conn: self._require_ready_volume_conn(conn, svm, volume))
                if require_ready_volume
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
    ) -> bool:
        return self._refresh_create_lease(
            "snapshots",
            {"svm": svm, "volume": volume, "name": name},
            owner,
            precondition=(
                (lambda conn: self._require_ready_volume_conn(conn, svm, volume))
                if require_ready_volume
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

    def reserve_snapshot_cleanup(self, svm: str, volume: str, name: str, owner: str) -> bool:
        now = datetime.now(timezone.utc)
        expires_at = (now + _SNAPSHOT_CLEANUP_RESERVATION_DURATION).isoformat()
        key = {"svm": svm, "volume": volume, "name": name}
        with self.transaction(immediate=True) as conn:
            self._prune_snapshot_cleanup_reservations_conn(conn, now.isoformat())
            if self._get_resource_by_key_conn(conn, "snapshots", key) is not None:
                return False
            try:
                conn.execute(
                    """INSERT INTO snapshot_cleanup_reservations (svm, volume, name, owner, expires_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (svm, volume, name, owner, expires_at, now.isoformat()),
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
        self._prune_snapshot_cleanup_reservations_conn(conn)
        cur = conn.execute(
            """SELECT 1 FROM snapshot_cleanup_reservations
               WHERE svm = ? AND volume = ? AND name = ?
               LIMIT 1
            """,
            (svm, volume, name),
        )
        return cur.fetchone() is not None

    def _prune_snapshot_cleanup_reservations_conn(
        self,
        conn: sqlite3.Connection,
        now: Optional[str] = None,
    ) -> None:
        conn.execute(
            "DELETE FROM snapshot_cleanup_reservations WHERE expires_at <= ?",
            (now or _now_iso(),),
        )

    # ---- Export operations ----

    def upsert_export(
        self,
        export: Any,
        *,
        expected_create_owner: Optional[str] = None,
        require_ready_volume: bool = False,
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
            self._require_ready_volume_conn(conn, export.spec.svm, export.spec.volume)
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
    ) -> Optional[dict[str, Any]]:
        return self._acquire_create_lease(
            "exports",
            {"svm": svm, "volume": volume, "client": client},
            owner,
            expected_spec=expected_spec,
            allow_failed=allow_failed,
        )

    def refresh_export_create_lease(self, svm: str, volume: str, client: str, owner: str) -> bool:
        return self._refresh_create_lease("exports", {"svm": svm, "volume": volume, "client": client}, owner)

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

    def _get_volume_conn(self, conn: sqlite3.Connection, svm: str, name: str) -> Optional[dict[str, Any]]:
        cur = conn.execute("SELECT * FROM volumes WHERE svm = ? AND name = ?", (svm, name))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_resource(row)

    def _require_ready_volume_conn(self, conn: sqlite3.Connection, svm: str, name: str) -> dict[str, Any]:
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

    @staticmethod
    def _snapshot_ref(snapshot: dict[str, Any]) -> str:
        spec = snapshot.get("spec", {})
        return f"{spec.get('svm')}/{spec.get('volume')}/{spec.get('name')}"

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
