# Changelog

All notable changes to the CSI ARCA Storage driver will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial implementation of CSI ARCA Storage driver
- Support for dynamic volume provisioning
- Support for volume snapshots (server-side reflink-based)
- Support for volume cloning
- Support for volume expansion
- Per-namespace SVM isolation
- Standalone network allocator with round-robin IP allocation
- Distributed locking using Kubernetes Leases
- Per-SVM shared NFS mounts with derived refcounting
- Node state persistence for crash recovery
- Idempotent volume and snapshot operations
- Comprehensive Kubernetes deployment manifests
- RBAC configurations for controller and node plugins
- Kustomize base and overlays (development/production)
- StorageClass and VolumeSnapshotClass examples
- Example manifests for PVC, pods, and snapshots
- Full deployment documentation
- Quick start guide
- Multi-stage Docker build
- Makefile for build automation
- Configuration validation
- TLS support with mTLS capability

### CSI Features Implemented
- CSI Identity Service
  - GetPluginInfo
  - GetPluginCapabilities
  - Probe
- CSI Controller Service
  - CreateVolume (with content source support)
  - DeleteVolume
  - ControllerPublishVolume (no-op for NFS)
  - ControllerUnpublishVolume (no-op for NFS)
  - ValidateVolumeCapabilities
  - ListVolumes
  - ControllerGetCapabilities
  - CreateSnapshot
  - DeleteSnapshot
  - ListSnapshots
  - ControllerExpandVolume
  - ControllerGetVolume (optional)
- CSI Node Service
  - NodeStageVolume (bind mount from per-SVM mount)
  - NodeUnstageVolume (with automatic SVM unmount)
  - NodePublishVolume (bind mount to pod)
  - NodeUnpublishVolume
  - NodeGetVolumeStats
  - NodeExpandVolume (no-op for NFS)
  - NodeGetCapabilities
  - NodeGetInfo

### Capabilities
- Volume Lifecycle: CREATE_DELETE_VOLUME
- Volume Expansion: EXPAND_VOLUME (controller-side only)
- Snapshots: CREATE_DELETE_SNAPSHOT
- Cloning: CLONE_VOLUME
- Access Modes: ReadWriteOnce, ReadOnlyMany, ReadWriteMany
- Volume Modes: Filesystem

### Architecture Components
- **ARCA API Client**: REST client with retry logic and error handling
- **SVM Manager**: Lifecycle management with distributed locking
- **Network Allocator**: Round-robin IP allocation from static pools
- **Mount Manager**: Per-SVM shared mount management with refcounting
- **Node State**: Persistent state with atomic writes and fsync
- **Lock Manager**: Kubernetes Lease-based distributed locks
- **Volume ID Generator**: Deterministic ID generation for idempotency
- **Snapshot ID Generator**: Deterministic snapshot ID generation
- **Configuration**: YAML-based configuration with validation

### Documentation
- [README.md](README.md): Project overview and features
- [README.ja.md](README.ja.md): 日本語 README
- [docs/quickstart.md](docs/quickstart.md): 10-minute quick start guide
- [docs/quickstart.ja.md](docs/quickstart.ja.md): 日本語クイックスタート
- [docs/deployment.md](docs/deployment.md): Comprehensive deployment guide
- [docs/deployment.ja.md](docs/deployment.ja.md): 日本語デプロイガイド
- [docs/deployment-checklist.md](docs/deployment-checklist.md): Deployment checklist
- [docs/deployment-checklist.ja.md](docs/deployment-checklist.ja.md): 日本語デプロイチェックリスト
- [config.example.yaml](config.example.yaml): Configuration examples

### Deployment
- CSIDriver manifest with volume lifecycle support
- Controller StatefulSet with external sidecars:
  - csi-provisioner v5.1.0
  - csi-snapshotter v8.1.0
  - csi-resizer v1.12.0
  - livenessprobe v2.14.0
- Node DaemonSet with external sidecars:
  - csi-node-driver-registrar v2.12.0
  - livenessprobe v2.14.0
- RBAC for controller and node plugins
- Kustomize support with development and production overlays
- ConfigMap-based configuration
- Secret-based authentication

### Build System
- Multi-stage Dockerfile (Go 1.23 + Alpine 3.19)
- Makefile with targets: build, test, docker-build, fmt, vet, tidy
- Go module dependencies management
- .gitignore for build artifacts and secrets

## [1.0.0] - TBD

Initial release

### Notes
- Requires Kubernetes 1.20 or later
- Requires ARCA storage backend with API access
- Requires CSI volume snapshots feature enabled for snapshot support
- Node plugin requires privileged containers for mount operations
