"""Runtime state path helpers for CLI compatibility."""

from __future__ import annotations

from pathlib import Path

from arca_storage.config import load_settings


def get_state_dir() -> Path:
    """Resolve the directory used for runtime state artifacts."""
    return Path(load_settings().state.runtime_dir)
