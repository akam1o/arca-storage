# Testing Guide

This document describes the test suite for Arca Storage.

## Test Structure

```
tests/
├── unit/              # Unit tests for individual functions
│   ├── test_validators.py
│   ├── test_netns.py
│   ├── test_lvm.py
│   ├── test_xfs.py
│   └── test_ganesha.py
├── integration/       # Integration and scenario tests
│   ├── test_cli_svm.py
│   ├── test_cli_volume.py
│   ├── test_cli_export.py
│   ├── test_api_svms.py
│   ├── test_api_volumes.py
│   ├── test_api_exports.py
│   └── test_scenarios.py
└── conftest.py        # Pytest fixtures and configuration
```

## Running Tests

### Install Test Dependencies

```bash
pip install -r requirements.txt
```

### Run All Tests

```bash
pytest
```

### Run Unit Tests Only

```bash
pytest tests/unit/
```

### Run Integration Tests Only

```bash
pytest tests/integration/
```

### Run Tests with Coverage

```bash
pytest --cov=arca_storage --cov-report=html
```

Coverage report will be generated in `htmlcov/index.html`.

### Run Specific Test File

```bash
pytest tests/unit/test_validators.py
```

### Run Specific Test

```bash
pytest tests/unit/test_validators.py::TestValidateName::test_valid_name
```

### Run Tests by Marker

```bash
# Run only unit tests
pytest -m unit

# Run only integration tests
pytest -m integration

# Run slow tests
pytest -m slow

# Skip tests requiring root
pytest -m "not requires_root"
```

## Test Categories

### Unit Tests

Unit tests test individual functions in isolation using mocks. They are fast and don't require system privileges.

- **Location**: `tests/unit/`
- **Marker**: `@pytest.mark.unit`
- **Examples**:
  - Input validation
  - Function logic
  - Error handling

### Integration Tests

Integration tests test the interaction between components, including CLI commands and API endpoints.

- **Location**: `tests/integration/`
- **Marker**: `@pytest.mark.integration`
- **Examples**:
  - CLI command execution
  - API endpoint responses
  - End-to-end workflows

### Scenario Tests

Scenario tests test complete workflows from start to finish.

- **Location**: `tests/integration/test_scenarios.py`
- **Marker**: `@pytest.mark.slow`
- **Examples**:
  - Full SVM lifecycle (create → use → delete)
  - Error recovery scenarios

## Test Fixtures

Common fixtures are defined in `tests/conftest.py`:

- `temp_dir`: Temporary directory for test files
- `mock_subprocess`: Mock for subprocess.run
- `mock_path_exists`: Mock for os.path.exists
- `mock_open`: Mock for file operations
- `client`: FastAPI test client

## Writing Tests

### Unit Test Example

```python
import pytest
from arca_storage.cli.lib.validators import validate_name

@pytest.mark.unit
def test_valid_name():
    """Test valid names."""
    validate_name("tenant_a")
    validate_name("tenant-1")
```

### Integration Test Example

```python
import pytest
from typer.testing import CliRunner
from arca_storage.cli.cli import app

@pytest.mark.integration
def test_create_svm_success():
    """Test successful SVM creation."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["svm", "create", "tenant_a", "--vlan", "100", "--ip", "192.168.10.5/24"]
    )
    assert result.exit_code == 0
```

### API Test Example

```python
import pytest
from fastapi.testclient import TestClient
from arca_storage.api.main import app

@pytest.fixture
def client():
    return TestClient(app)

@pytest.mark.integration
def test_create_svm_api(client):
    """Test SVM creation via API."""
    response = client.post(
        "/v1/svms",
        json={
            "name": "tenant_a",
            "vlan_id": 100,
            "ip_cidr": "192.168.10.5/24"
        }
    )
    assert response.status_code == 201
```

## Mocking

Tests use mocks to avoid requiring actual system resources:

- **subprocess.run**: Mocked to avoid executing actual system commands
- **File operations**: Mocked to avoid file system access
- **Network operations**: Mocked to avoid network access

## Continuous Integration

Tests should be run in CI/CD pipelines:

```yaml
# Example GitHub Actions workflow
- name: Run tests
  run: |
    pip install -r requirements.txt
    pytest --cov=arca_storage --cov-report=xml
```

## Test Coverage Goals

- **Unit tests**: >80% coverage for lib functions
- **Integration tests**: Cover all CLI commands and API endpoints
- **Scenario tests**: Cover critical workflows

## Troubleshooting

### Tests Fail with Import Errors

Ensure you're running tests from the project root:

```bash
cd /path/to/arca-storage
pytest
```

### Async Tests Fail

Ensure `pytest-asyncio` is installed and `asyncio_mode = auto` is set in `pytest.ini`.

### Mock Not Working

Check that you're patching the correct import path. Use the path where the function is used, not where it's defined.

### Coverage Report Not Generated

Ensure `pytest-cov` is installed and `--cov` flags are used.
