# Arca Storage

[![CI](https://github.com/akam1o/arca-storage/actions/workflows/ci.yml/badge.svg)](https://github.com/akam1o/arca-storage/actions/workflows/ci.yml)
[![Python Tests](https://github.com/akam1o/arca-storage/actions/workflows/python-tests.yml/badge.svg)](https://github.com/akam1o/arca-storage/actions/workflows/python-tests.yml)
[![Ansible Lint](https://github.com/akam1o/arca-storage/actions/workflows/ansible-lint.yml/badge.svg)](https://github.com/akam1o/arca-storage/actions/workflows/ansible-lint.yml)

Software-Defined Storage system with Storage Virtual Machine (SVM) functionality, built using Linux standard technologies.

## Overview

Arca Storage is a Software-Defined Storage system that provides NetApp ONTAP-like SVM functionality using Linux standard technologies:

- **Multi-protocol**: NFS v4.1 / v4.2 (default), with optional NFSv3 support
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

- RHEL/Alma/Rocky Linux 8/9, Debian, or Ubuntu
- Pacemaker/Corosync/pcs, NFS-Ganesha, LVM2, DRBD installed
- 2-node cluster configuration

### Installation

1. **Install OS dependencies (example):**

   ```bash
   # EL9 (RHEL/Alma/Rocky 9)
   sudo dnf install -y pacemaker corosync pcs resource-agents \
     nfs-ganesha nfs-ganesha-utils \
     lvm2 xfsprogs \
     drbd-utils drbd-kmod

   # Debian/Ubuntu (package names may vary)
   sudo apt-get update
   sudo apt-get install -y pacemaker corosync pcs resource-agents \
     nfs-ganesha \
     lvm2 xfsprogs \
     drbd-utils
   ```

2. **Install `arca-storage` package (rpm/deb):**

   Download the latest package from GitHub Releases and install it.

   ```bash
   # EL9 (rpm)
   sudo dnf install -y ./arca-storage-*.rpm

   # Debian/Ubuntu (deb)
   sudo apt-get install -y ./arca-storage_*.deb
   ```

3. **Follow MVP setup guide:**

   See [docs/mvp-setup.md](docs/mvp-setup.md) for detailed setup instructions.

## Configuration

### NFSv3 Support (Optional)

By default, Arca Storage uses NFSv4 only. To enable NFSv3 support:

1. **Edit runtime config:**

   In `/etc/arca-storage/storage-runtime.conf`:

   ```ini
   [storage]
   # Enable NFSv3 (use both v3 and v4)
   ganesha_protocols = 3,4

   # Fixed ports (recommended when using NFSv3)
   ganesha_mountd_port = 20048
   ganesha_nlm_port = 32768
   ```

2. **Re-render configs and reload services:**

   ```bash
   # Keep env file in sync (optional but recommended after config edits)
   sudo arca bootstrap render-env

   # Re-render per-SVM ganesha.conf and reload
   sudo arca export sync --all
   ```

3. **Required firewall ports when NFSv3 is enabled:**

   ```
   111/tcp,udp   (rpcbind/portmapper)
   2049/tcp,udp  (NFS)
   20048/tcp,udp (mountd)
   32768/tcp,udp (NLM)
   ```

4. **Client mount examples:**

   ```bash
   # NFSv4 (default)
   mount -t nfs4 server:/101 /mnt

   # NFSv3 (when enabled)
   mount -t nfs -o vers=3 server:/exports /mnt
   ```

**Note**: When using NFSv3, ensure `rpcbind` is installed and running. Both NFSv3 and NFSv4 protocols will be available simultaneously.

## Usage

### CLI Tool (arca)

```bash
# Bootstrap (without Ansible)
arca bootstrap install

# (Optional) edit configs
sudo vi /etc/arca-storage/storage-bootstrap.conf
sudo vi /etc/arca-storage/storage-runtime.conf

# Re-generate /etc/arca-storage/arca-storage.env after editing configs
arca bootstrap render-env

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
arca-storage-api --host 127.0.0.1 --port 8080
```

Or run it as a systemd service (when installed via package):

```bash
sudo systemctl enable --now arca-storage-api
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
│   ├── arca_storage/           # Package source code
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
- [arca_storage/arca_storage/resources/pacemaker/](arca_storage/arca_storage/resources/pacemaker/) - Pacemaker RA documentation
- [arca_storage/arca_storage/resources/systemd/](arca_storage/arca_storage/resources/systemd/) - systemd unit files
- [arca_storage/arca_storage/templates/](arca_storage/arca_storage/templates/) - Template documentation

## License

Apache License 2.0

## Contributing

Contributions are welcome! Please read the specification and follow the coding standards.

## Status

This project is in active development. The MVP implementation is complete, but additional features and optimizations are planned.
