"""Pagination helpers for CLI list commands."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from arca_storage.db import encode_cursor

Record = dict[str, Any]

_PAGE_SIZE = 100


def _collect_pages(
    fetch_page: Callable[[str | None], list[Record]],
    cursor_values: Callable[[Record], list[str]],
) -> list[Record]:
    records: list[Record] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while True:
        page = fetch_page(cursor)
        if not page:
            return records

        records.extend(page)
        if len(page) < _PAGE_SIZE:
            return records

        cursor = encode_cursor(cursor_values(page[-1]))
        if cursor in seen_cursors:
            raise RuntimeError("repeated pagination cursor while listing resources")
        seen_cursors.add(cursor)


def list_all_svms(db: Any) -> list[Record]:
    return _collect_pages(
        lambda cursor: db.list_svms(limit=_PAGE_SIZE, cursor=cursor),
        lambda record: [str(record["spec"]["name"])],
    )


def list_all_volumes(
    db: Any,
    *,
    svm: str | None = None,
    name: str | None = None,
) -> list[Record]:
    return _collect_pages(
        lambda cursor: db.list_volumes(
            svm=svm, name=name, limit=_PAGE_SIZE, cursor=cursor
        ),
        lambda record: [str(record["spec"]["svm"]), str(record["spec"]["name"])],
    )


def list_all_exports(
    db: Any,
    *,
    svm: str | None = None,
    volume: str | None = None,
) -> list[Record]:
    return _collect_pages(
        lambda cursor: db.list_exports(
            svm=svm, volume=volume, limit=_PAGE_SIZE, cursor=cursor
        ),
        lambda record: [
            str(record["spec"]["svm"]),
            str(record["spec"]["volume"]),
            str(record["spec"]["client"]),
        ],
    )
