"""
Pytest configuration and fixtures.
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


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
