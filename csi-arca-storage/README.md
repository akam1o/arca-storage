# CSI Driver for ARCA Storage

English | [日本語](README.ja.md)

A Kubernetes CSI (Container Storage Interface) driver for ARCA Storage, providing dynamic provisioning of persistent volumes backed by XFS with reflink-based snapshots.

## Features

- **Dynamic Volume Provisioning**: Automatic volume creation with XFS project quotas
- **Snapshots**: Server-side reflink-based snapshots for instant, space-efficient copies
- **Volume Cloning**: Clone volumes from existing volumes or snapshots
- **Volume Expansion**: Online volume expansion through quota updates
- **Per-Namespace SVM Isolation**: Each Kubernetes namespace gets its own Storage Virtual Machine
- **Multi-Access Modes**: Support for ReadWriteOnce, ReadOnlyMany, and ReadWriteMany
- **Idempotent Operations**: All operations are designed to be safely retryable

## Architecture

The CSI driver consists of two main components:

1. **Controller Plugin**: Handles volume lifecycle operations (create, delete, snapshot, expand)
2. **Node Plugin**: Manages volume mounting on worker nodes

### Key Components

- **ARCA API Client**: REST API client for interacting with ARCA storage backend
- **SVM Manager**: Manages Storage Virtual Machine lifecycle with distributed locking
- **Network Allocator**: Round-robin IP allocation from configured pools
- **Mount Manager**: Per-SVM shared NFS mounts with derived refcounting
- **Node State**: Persistent state management for crash recovery
- **CRD Metadata Store**: Controller metadata persisted as `ArcaVolume` and `ArcaSnapshot` custom resources
- **Structured Error Handling**: The API client parses structured error responses from the ARCA backend (error codes like `NOT_FOUND`, `ALREADY_EXISTS`, `CONFLICT`) and maps them to appropriate gRPC status codes for Kubernetes

## Building

Use Go 1.26.3, matching the `go` version declared in `go.mod`.

```bash
go mod download
go build -o bin/csi-driver ./cmd/csi-driver
```

## Configuration

Create a configuration file at `/etc/csi-arca-storage/config.yaml`:

```yaml
arca:
  base_url: "https://arca-api.example.com"
  timeout: "30s"
  auth_type: "token"
  auth_token: "your-auth-token"
  tls:
    ca_cert_path: "/etc/csi-arca-storage/ca.crt"
    insecure_skip_verify: false

network:
  pools:
    - cidr: "10.0.0.0/24"
      range: "10.0.0.100-10.0.0.200"
      vlan: 100       # Optional; omit for non-VLAN SVMs
      gateway: "10.0.0.1"  # Optional for VLAN-backed /30 or larger CIDRs
  mtu: 1500

driver:
  node_id: ""  # Auto-detected from hostname if not set
  endpoint: "unix:///csi/csi.sock"
  state_file_path: "/var/lib/csi-arca-storage/node-volumes.json"
  base_mount_path: "/var/lib/kubelet/plugins/csi.arca-storage.io/mounts"
```

In Kubernetes deployments, pass the API token from a Secret as `ARCA_AUTH_TOKEN`. The driver uses that environment variable to override `arca.auth_token` from the ConfigMap. Set `arca.auth_type: "none"` only when the ARCA API server is explicitly configured for unauthenticated access.

## Deployment

### Quick Start

For a quick deployment guide, see [docs/quickstart.md](docs/quickstart.md).

### Full Deployment Guide

For detailed deployment instructions, configuration options, and production best practices, see [docs/deployment.md](docs/deployment.md).

### Prerequisites

- Kubernetes 1.25+
- ARCA Storage backend with API access
- Network connectivity between nodes and ARCA storage network

The bundled driver CRDs use Kubernetes CEL validation, so Kubernetes 1.25+ is the practical minimum for the manifests in `deploy/crds/`.

### Installation Methods

#### Method 1: Direct kubectl (Quickstart)

```bash
# Install driver CRDs first (required before controller starts)
kubectl apply -k deploy/crds/

# If you plan to use VolumeSnapshot/VolumeSnapshotClass, ensure Kubernetes snapshot CRDs are installed first.

# Create secret with ARCA API token
kubectl create secret generic csi-arca-storage-secret \
  --namespace=kube-system \
  --from-literal=auth-token='your-token'

# Deploy driver components
kubectl apply -f deploy/csidriver.yaml
kubectl apply -f deploy/rbac-controller.yaml
kubectl apply -f deploy/rbac-node.yaml
kubectl apply -f deploy/controller.yaml
kubectl apply -f deploy/node.yaml

# Deploy storage classes
kubectl apply -f deploy/examples/storageclass.yaml
kubectl apply -f deploy/examples/volumesnapshotclass.yaml
```

#### Method 2: Kustomize (Recommended for Production)

```bash
# Development
kubectl apply -k deploy/kustomize/overlays/development

# Production
# Create deploy/kustomize/overlays/production/secrets.env first.
kubectl apply -k deploy/kustomize/overlays/production
```

See [docs/deployment.md](docs/deployment.md) for configuration details

### Storage Class

Create a StorageClass to use the CSI driver:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: arca-storage
provisioner: csi.arca-storage.io
parameters:
  # No parameters needed - namespace is automatically used
reclaimPolicy: Delete
volumeBindingMode: Immediate
allowVolumeExpansion: true
```

### Volume Snapshot Class

Create a VolumeSnapshotClass for snapshots:

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotClass
metadata:
  name: arca-snapshots
driver: csi.arca-storage.io
deletionPolicy: Delete
```

## Usage Examples

### Creating a PersistentVolumeClaim

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-pvc
  namespace: default
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: arca-storage
  resources:
    requests:
      storage: 10Gi
```

### Creating a Snapshot

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: my-snapshot
  namespace: default
spec:
  volumeSnapshotClassName: arca-snapshots
  source:
    persistentVolumeClaimName: my-pvc
```

### Cloning from a Snapshot

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-clone
  namespace: default
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: arca-storage
  dataSource:
    name: my-snapshot
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
  resources:
    requests:
      storage: 10Gi
```

### Volume Expansion

```yaml
# Edit the PVC to increase the storage size
kubectl edit pvc my-pvc

# Update spec.resources.requests.storage to the new size
spec:
  resources:
    requests:
      storage: 20Gi  # Increased from 10Gi
```

## Development

### Project Structure

```
csi-arca-storage/
├── cmd/
│   └── csi-driver/          # Main entry point
├── pkg/
│   ├── arca/                # ARCA API client and managers
│   │   ├── client.go        # REST API client
│   │   ├── directory.go     # Directory operations
│   │   ├── snapshot.go      # Snapshot operations
│   │   ├── quota.go         # XFS quota management
│   │   ├── network.go       # Network allocator
│   │   ├── svm.go           # SVM lifecycle manager
│   │   ├── types.go         # API types
│   │   └── errors.go        # Error handling
│   ├── driver/              # CSI driver implementation
│   │   ├── driver.go        # Driver core
│   │   ├── identity.go      # CSI Identity service
│   │   ├── controller.go    # CSI Controller service
│   │   ├── node.go          # CSI Node service
│   │   └── version.go       # Version constants
│   ├── mount/               # Mount management
│   │   ├── manager.go       # Mount manager
│   │   ├── node_state.go    # Node state persistence
│   │   └── nfs.go           # NFS utilities
│   ├── idempotency/         # ID generation
│   │   ├── volume.go        # Volume ID generator
│   │   └── snapshot.go      # Snapshot ID generator
│   ├── lock/                # Distributed locking
│   │   └── manager.go       # Kubernetes Lease-based locks
│   ├── config/              # Configuration
│   │   └── config.go        # Config loading and validation
│   ├── store/               # Metadata storage (CRD-backed + cache)
│   └── apis/                # ArcaVolume / ArcaSnapshot API types
└── deploy/                  # Kubernetes manifests
```

### Running Tests

Use Go 1.26.3 for local test runs so the toolchain matches `go.mod`.

```bash
go test ./...
```

### Building Container Image

```bash
docker build -t csi-arca-storage:latest .
```

## Troubleshooting

### Check Driver Status

```bash
# Check controller pod logs
kubectl logs -n kube-system -l app=csi-arca-storage-controller

# Check node plugin logs
kubectl logs -n kube-system -l app=csi-arca-storage-node

# Check CSIDriver object
kubectl get csidriver csi.arca-storage.io
```

### Common Issues

1. **Volume creation fails**: Check ARCA API connectivity and authentication
2. **Mount failures**: Verify network connectivity to storage VIP
3. **SVM conflicts**: Check for IP collisions, and VLAN collisions when VLANs are configured in network pools
4. **Snapshot failures**: Ensure XFS reflink support on ARCA backend

## License

[LICENSE](../LICENSE)

## Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md)
