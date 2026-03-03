# Arca Storage - Python Package

Software-Defined Storage system with SVM (Storage Virtual Machine) functionality.

## Directory Structure

```
arca_storage/
├── arca_storage/        # Main package source code
│   ├── api/             # REST API (FastAPI)
│   │   ├── main.py      # API application & error handlers
│   │   ├── models.py    # Request/Response Pydantic models
│   │   └── services/    # Service layer (delegates to reconcilers)
│   ├── cli/             # CLI tool (Typer)
│   │   ├── cli.py       # Main CLI entry
│   │   ├── commands/    # Command implementations
│   │   └── lib/         # Validators, helpers
│   ├── models/          # Resource models (Spec/Status, Pydantic v2)
│   ├── reconcilers/     # Reconciliation loops (desired → actual state)
│   ├── adapters/        # Protocol-based system operation adapters
│   ├── db/              # SQLite WAL state store
│   ├── errors.py        # Structured error codes & hierarchy
│   ├── config.py        # TOML configuration (Pydantic)
│   ├── context.py       # Application context / dependency wiring
│   ├── openstack/       # OpenStack drivers (Cinder, Manila)
│   ├── templates/       # Configuration templates
│   └── resources/       # Pacemaker RA, systemd units
├── tests/               # Test suite
│   ├── unit/            # Unit tests (models, errors, db, reconcilers)
│   └── integration/     # Integration tests (CLI, API, scenarios)
├── pyproject.toml       # Modern package configuration
├── pytest.ini           # Pytest configuration
└── requirements.txt     # Python dependencies
```

## Installation

### Development Installation

```bash
cd arca_storage
pip install -e ".[dev]"
```

### Production Installation

```bash
cd arca_storage
pip install .
```

## Usage

### CLI Tool

```bash
# Show help
arca --help

# SVM management
arca svm create <name> --vlan <id> --ip <cidr>
arca svm list
arca svm delete <name>

# Volume management
arca volume create <name> --svm <svm_name> --size <gib>
arca volume list --svm <svm_name>
arca volume delete <name> --svm <svm_name>

# Export management
arca export add --volume <name> --svm <svm_name> --client <cidr>
arca export list --svm <svm_name>
arca export remove --volume <name> --svm <svm_name> --client <cidr>
```

### REST API

```bash
# Start API server
arca-storage-api --host 127.0.0.1 --port 8080

# API will be available at http://localhost:8080
# API documentation: http://localhost:8080/docs
```

## Testing

```bash
cd arca_storage

# Run all tests
pytest

# Run specific test file
pytest tests/unit/test_validators.py

# Run with coverage
pytest --cov=arca_storage --cov-report=html
```

## Development

### Code Structure

The codebase follows a **declarative reconciliation** architecture:

- **Models** (`arca_storage/models/`): Pydantic v2 resource models with Spec (desired state) and Status (actual state) for SVM, Volume, Snapshot, Export.
- **Reconcilers** (`arca_storage/reconcilers/`): Idempotent loops that drive resources from desired to actual state, persisting each step for crash-safe retries.
- **Adapters** (`arca_storage/adapters/`): Protocol-based abstractions for LVM, XFS, Network Namespace, Pacemaker, NFS-Ganesha, systemd. Each has a Subprocess (production) and Fake (testing) implementation.
- **State Store** (`arca_storage/db/`): SQLite WAL-backed database with ACID transactions.
- **Errors** (`arca_storage/errors.py`): Structured error codes mapping to HTTP status codes.
- **Config** (`arca_storage/config.py`): TOML-based configuration validated with Pydantic.
- **Context** (`arca_storage/context.py`): `AppContext` wiring DB, adapters, and reconcilers.
- **CLI** (`arca_storage/cli/`): Command-line interface using Typer.
- **API** (`arca_storage/api/`): REST API using FastAPI with global `ArcaError` exception handler.

### Dependencies

**Runtime:**
- typer >= 0.9.0
- click >= 8.1.0
- fastapi >= 0.104.0
- uvicorn >= 0.24.0
- pydantic >= 2.5.0
- jinja2 >= 3.1.0

**Development:**
- pytest >= 7.4.0
- pytest-asyncio >= 0.21.0
- pytest-cov >= 4.1.0
- pytest-mock >= 3.12.0
- httpx >= 0.25.0

## License

Apache License 2.0
