# Arca Storage - Python Package

Software-Defined Storage system with SVM (Storage Virtual Machine) functionality.

## Directory Structure

```
arca_storage/
├── arca_storage/        # Main package source code
│   ├── api/             # REST API (FastAPI)
│   ├── cli/             # CLI tool (Typer)
│   ├── templates/       # Configuration templates
│   └── resources/       # Resource files
├── tests/               # Test suite
│   ├── unit/            # Unit tests
│   └── integration/     # Integration tests
├── pyproject.toml       # Modern package configuration
├── setup.py             # Legacy package configuration
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
arca svm create <name>
arca svm list
arca svm delete <name>

# Volume management
arca volume create <svm_name> <volume_name> <size>
arca volume list <svm_name>
arca volume delete <svm_name> <volume_name>

# Export management
arca export create <svm_name> <volume_name> <client_cidr>
arca export list <svm_name>
arca export delete <svm_name> <export_id>
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

- **CLI** (`arca_storage/cli/`): Command-line interface using Typer
- **API** (`arca_storage/api/`): REST API using FastAPI
- **Lib** (`arca_storage/cli/lib/`): Core functionality modules
  - `ganesha.py`: NFS-Ganesha configuration
  - `lvm.py`: LVM management
  - `netns.py`: Network namespace management
  - `pacemaker.py`: Pacemaker resource management
  - `systemd.py`: Systemd service management
  - `validators.py`: Input validation
  - `xfs.py`: XFS filesystem management

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
