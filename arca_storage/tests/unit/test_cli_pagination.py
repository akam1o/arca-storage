"""Tests for CLI pagination helpers."""

import pytest

from arca_storage.cli.commands import _pagination


class RepeatingCursorDB:
    def list_svms(self, *, limit, cursor=None):
        assert limit == 1
        return [{"spec": {"name": "tenant_a"}}]


def test_list_all_svms_rejects_repeated_cursor(monkeypatch):
    monkeypatch.setattr(_pagination, "_PAGE_SIZE", 1)

    with pytest.raises(RuntimeError, match="repeated pagination cursor"):
        _pagination.list_all_svms(RepeatingCursorDB())
