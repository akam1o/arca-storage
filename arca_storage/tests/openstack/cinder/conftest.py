from __future__ import annotations

from importlib.util import find_spec


_CINDER_AVAILABLE = find_spec("cinder") is not None


def pytest_ignore_collect(collection_path, config):
    if _CINDER_AVAILABLE:
        return False

    return "integration" in collection_path.parts
