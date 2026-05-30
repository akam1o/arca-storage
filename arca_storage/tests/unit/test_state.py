"""Unit tests for CLI state path helpers."""

import pytest


@pytest.mark.unit
def test_get_state_dir_uses_configured_runtime_dir(temp_dir, monkeypatch):
    config_path = temp_dir / "config.toml"
    config_path.write_text(f'[state]\nruntime_dir = "{temp_dir}"\n', encoding="utf-8")
    monkeypatch.setenv("ARCA_CONFIG_PATH", str(config_path))

    from arca_storage.cli.lib import state

    assert state.get_state_dir() == temp_dir


@pytest.mark.unit
def test_legacy_json_state_helpers_are_removed():
    from arca_storage.cli.lib import state

    legacy_helpers = {
        "list_svms",
        "upsert_svm",
        "delete_svm",
        "list_volumes",
        "upsert_volume",
        "delete_volume",
        "list_snapshots",
        "upsert_snapshot",
        "delete_snapshot",
    }
    assert legacy_helpers.isdisjoint(dir(state))
