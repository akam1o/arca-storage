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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from arca_storage.errors import AlreadyExistsError, NotFoundError
from arca_storage.models.base import Phase


_SCHEMA_VERSION = 1

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


def encode_cursor(values: list[str]) -> str:
    payload = json.dumps(values, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None, expected_parts: int) -> list[str] | None:
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

    def __init__(self, db_path: Path | str) -> None:
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

    def upsert_svm(self, svm: Any) -> None:
        """Insert or update an SVM record."""
        now = _now_iso()
        with self.transaction() as conn:
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

    def get_svm(self, name: str) -> dict[str, Any] | None:
        conn = self._conn()
        cur = conn.execute("SELECT * FROM svms WHERE name = ?", (name,))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_resource(row)

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

    def upsert_volume(self, volume: Any) -> None:
        now = _now_iso()
        with self.transaction() as conn:
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

    def get_volume(self, svm: str, name: str) -> dict[str, Any] | None:
        conn = self._conn()
        cur = conn.execute("SELECT * FROM volumes WHERE svm = ? AND name = ?", (svm, name))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_resource(row)

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

    # ---- Snapshot operations ----

    def upsert_snapshot(self, snapshot: Any) -> None:
        now = _now_iso()
        with self.transaction() as conn:
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

    def list_snapshots(
        self,
        svm: Optional[str] = None,
        volume: Optional[str] = None,
        name: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        conn = self._conn()
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

    # ---- Export operations ----

    def upsert_export(self, export: Any) -> None:
        now = _now_iso()
        with self.transaction() as conn:
            self._upsert_export_conn(conn, export, now=now)

    def _upsert_export_conn(self, conn: sqlite3.Connection, export: Any, *, now: Optional[str] = None) -> None:
        now = now or _now_iso()
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

    def get_export(self, svm: str, volume: str, client: str) -> dict[str, Any] | None:
        conn = self._conn()
        return self._get_export_conn(conn, svm, volume, client)

    def _get_export_conn(
        self,
        conn: sqlite3.Connection,
        svm: str,
        volume: str,
        client: str,
    ) -> dict[str, Any] | None:
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
