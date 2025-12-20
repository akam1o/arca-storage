# Arca Storage

[![CI](https://github.com/akam1o/arca-storage/actions/workflows/ci.yml/badge.svg)](https://github.com/akam1o/arca-storage/actions/workflows/ci.yml)
[![Python Tests](https://github.com/akam1o/arca-storage/actions/workflows/python-tests.yml/badge.svg)](https://github.com/akam1o/arca-storage/actions/workflows/python-tests.yml)
[![Ansible Lint](https://github.com/akam1o/arca-storage/actions/workflows/ansible-lint.yml/badge.svg)](https://github.com/akam1o/arca-storage/actions/workflows/ansible-lint.yml)

Software-Defined Storage system with Storage Virtual Machine (SVM) functionality, built using Linux standard technologies.

## Overview

Arca Storage is a Software-Defined Storage system that provides NetApp ONTAP-like SVM functionality using Linux standard technologies:

- **Multi-protocol**: NFS v4.1 / v4.2
- **Multi-tenancy**: Network Namespace-based network isolation
- **High Availability**: Pacemaker-based Active/Active failover
- **Data Efficiency**: LVM Thin Provisioning with overcommit
- **Client Integration**: Kubernetes (CSI) and OpenStack (Cinder NFS Driver) support

## Architecture

The system combines:
- **Pacemaker + Corosync**: HA clustering and resource management
- **NFS-Ganesha**: User-space NFS server (one process per SVM)
- **Network Namespace**: Tenant network isolation
- **XFS**: NVMe-optimized filesystem
- **LVM Thin Provisioning**: Virtual volume management and snapshots
- **DRBD**: Node-to-node synchronous data mirroring

## Quick Start

### Prerequisites

- RHEL/Alma/Rocky Linux 8 or 9
- Pacemaker, Corosync, NFS-Ganesha, LVM2, DRBD installed
- 2-node cluster configuration

### Installation

1. **Install Python package:**

   ```bash
   cd arca_storage
   pip install -e ".[dev]"
   ```

2. **Install Pacemaker Resource Agent:**

   ```bash
   sudo cp arca_storage/src/arca_storage/resources/pacemaker/NetnsVlan /usr/lib/ocf/resource.d/local/NetnsVlan
   sudo chmod +x /usr/lib/ocf/resource.d/local/NetnsVlan
   ```

3. **Follow MVP setup guide:**

   See [docs/mvp-setup.md](docs/mvp-setup.md) for detailed setup instructions.

## Usage

### CLI Tool (arca)

```bash
# Create an SVM
arca svm create tenant_a --vlan 100 --ip 192.168.10.5/24 --gateway 192.168.10.1

# Create a volume
arca volume create vol1 --svm tenant_a --size 100

# Add an export
arca export add --volume vol1 --svm tenant_a --client 10.0.0.0/24 --rw

# List SVMs
arca svm list
```

### REST API

Start the API server:

```bash
uvicorn arca_storage.api.main:app --host 0.0.0.0 --port 8080
```

API endpoints:

- `POST /v1/svms` - Create SVM
- `GET /v1/svms` - List SVMs
- `DELETE /v1/svms/{name}` - Delete SVM
- `POST /v1/volumes` - Create volume
- `PATCH /v1/volumes/{name}` - Resize volume
- `DELETE /v1/volumes/{name}` - Delete volume
- `POST /v1/exports` - Add export
- `GET /v1/exports` - List exports
- `DELETE /v1/exports` - Remove export

See API documentation at `http://localhost:8080/docs` when the server is running.

## Project Structure

```
arca-storage/
├── arca_storage/               # Python package
│   ├── src/arca_storage/       # Package source code
│   │   ├── api/                # FastAPI REST API
│   │   │   ├── main.py         # API application
│   │   │   ├── models.py       # Pydantic models
│   │   │   └── services/       # Service layer
│   │   ├── cli/                # CLI tool
│   │   │   ├── cli.py          # Main CLI entry
│   │   │   ├── commands/       # Command implementations
│   │   │   └── lib/            # Library functions
│   │   ├── resources/          # System resources
│   │   │   └── pacemaker/      # Pacemaker resource agents
│   │   └── templates/          # Configuration templates
│   ├── tests/                  # Test suite
│   ├── pyproject.toml          # Package configuration
│   ├── setup.py                # Legacy configuration
│   ├── pytest.ini              # Test configuration
│   └── README.md               # Package documentation
├── ansible/                    # Ansible playbooks
│   ├── roles/                  # Ansible roles
│   └── site.yml                # Main playbook
├── docs/                       # Project documentation
│   └── mvp-setup.md            # MVP setup guide
└── README.md                   # This file
```

## Development

### Setup Development Environment

```bash
# Clone repository
git clone https://github.com/akam1o/arca-storage.git
cd arca-storage/arca_storage

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install in development mode with dev dependencies
pip install -e ".[dev]"
```

### Running Tests

```bash
cd arca_storage

# Run all tests
pytest

# Run with coverage
pytest --cov=arca_storage --cov-report=html

# Run specific tests
pytest tests/unit/
pytest tests/integration/
```

### Code Style

Follow PEP 8 for Python code.

## Documentation

- [docs/mvp-setup.md](docs/mvp-setup.md) - MVP setup guide
- [arca_storage/src/arca_storage/resources/pacemaker/](arca_storage/src/arca_storage/resources/pacemaker/) - Pacemaker RA documentation
- [arca_storage/src/arca_storage/templates/](arca_storage/src/arca_storage/templates/) - Template documentation

## License

Apache License 2.0

## Contributing

Contributions are welcome! Please read the specification and follow the coding standards.

## Status

This project is in active development. The MVP implementation is complete, but additional features and optimizations are planned.
