"""Unit tests for application context wiring."""

from datetime import datetime, timedelta, timezone

import pytest

from arca_storage.config import ArcaSettings, StateConfig
from arca_storage.context import AppContext
from arca_storage.db import StateDB


@pytest.mark.unit
def test_app_context_prunes_operation_log_on_startup(tmp_path):
    db_path = tmp_path / "state.db"
    state = StateDB(db_path)
    now = datetime.now(timezone.utc)
    with state.transaction(immediate=True) as conn:
        conn.execute(
            """INSERT INTO operation_log
               (resource_type, resource_id, operation, phase, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "SVM",
                "old",
                "create",
                "failed",
                "",
                (now - timedelta(days=2)).isoformat(),
            ),
        )
        conn.execute(
            """INSERT INTO operation_log
               (resource_type, resource_id, operation, phase, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("SVM", "fresh", "create", "completed", "", now.isoformat()),
        )
    state.close()

    settings = ArcaSettings(
        state=StateConfig(
            db_path=str(db_path),
            runtime_dir=str(tmp_path / "runtime"),
            operation_log_retention_days=1,
        )
    )
    ctx = AppContext(settings)
    try:
        assert [entry["resource_id"] for entry in ctx.db.list_operation_log()] == [
            "fresh"
        ]
    finally:
        ctx.close()
