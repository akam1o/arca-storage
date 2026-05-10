"""
Pytest configuration and fixtures.
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root_candidates() -> list[Path]:
    candidates: list[Path] = []
    if workspace := os.environ.get("GITHUB_WORKSPACE"):
        candidates.append(Path(workspace).resolve())

    cwd = Path.cwd().resolve()
    candidates.append(cwd)
    candidates.extend(cwd.parents)
    candidates.extend(Path(__file__).resolve().parents)
    return candidates


@pytest.fixture
def repo_root():
    """Locate the repository root even when CI copies tests to /tmp."""
    for candidate in _repo_root_candidates():
        if (candidate / "ansible").is_dir() and (candidate / "arca_storage").is_dir():
            return candidate

    raise FileNotFoundError("could not locate repository root")


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    temp_path = tempfile.mkdtemp()
    yield Path(temp_path)
    shutil.rmtree(temp_path)


@pytest.fixture
def mock_subprocess():
    """Mock subprocess.run for testing."""
    with patch("subprocess.run") as mock:
        yield mock


@pytest.fixture
def mock_path_exists():
    """Mock os.path.exists for testing."""
    with patch("os.path.exists") as mock:
        yield mock


@pytest.fixture
def mock_open():
    """Mock open() for file operations."""
    with patch("builtins.open", create=True) as mock:
        yield mock


@pytest.fixture
def mock_json_load():
    """Mock json.load for testing."""
    with patch("json.load") as mock:
        yield mock


@pytest.fixture
def mock_json_dump():
    """Mock json.dump for testing."""
    with patch("json.dump") as mock:
        yield mock


@pytest.fixture
def mock_template_render():
    """Mock Jinja2 Template.render for testing."""
    with patch("jinja2.Template.render") as mock:
        yield mock
