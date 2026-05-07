"""Tests for the API server entrypoint."""

import pytest

from arca_storage.api import server


def test_help_does_not_require_config(monkeypatch, capsys):
    def fail_load_settings():
        raise AssertionError("load_settings should not be called for --help")

    monkeypatch.setattr(server, "load_settings", fail_load_settings)

    with pytest.raises(SystemExit) as exc:
        server.main(["--help"])

    assert exc.value.code == 0
    assert "arca-storage-api" in capsys.readouterr().out
