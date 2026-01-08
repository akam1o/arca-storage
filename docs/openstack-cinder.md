# OpenStack Cinder (NFS) integration

This repository includes a Cinder NFS driver that uses an ARCA SVM export as a shared NFS share and stores volumes as files.

## Overview

- One SVM corresponds to one NFS export: `server:/exports/<svm>`
- Each Cinder volume becomes a file: `volume-<volume_id>`
- Snapshots/clones are file copies: `snapshot-<snapshot_id>`
- This driver is designed to work as a file-only backend. ARCA REST API usage is optional.

## Install / deploy

Make the Python package available on Cinder volume hosts (and any host that imports the driver):

- Install the package from your build artifact (rpm/deb) or `pip install .`
- Ensure the module path is importable: `arca_storage.openstack.cinder.driver`

## Cinder configuration

Example `cinder.conf` (multi-backend):

```ini
[DEFAULT]
enabled_backends = arca

[arca]
volume_driver = arca_storage.openstack.cinder.driver.ArcaStorageNFSDriver
volume_backend_name = arca_storage
driver_volume_type = nfs

# NFS/file-only mode (recommended)
arca_storage_use_api = false
arca_storage_nfs_server = 192.168.10.5
arca_storage_nfs_mount_point_base = /var/lib/cinder/mnt
arca_storage_nfs_mount_options = rw,noatime,nodiratime,vers=4.1

# SVM mapping strategy
arca_storage_svm_strategy = shared
arca_storage_default_svm = tenant_a

# Snapshot/clone copy timeout (seconds)
arca_storage_snapshot_copy_timeout = 600
```

If you want to resolve the NFS server via ARCA REST API (optional):

```ini
arca_storage_use_api = true
arca_storage_api_endpoint = http://127.0.0.1:8080
arca_storage_api_timeout = 30
arca_storage_api_retry_count = 3
arca_storage_verify_ssl = true
```

## SVM mapping strategies

### `shared`

All volumes use `arca_storage_default_svm`.

### `manual`

Choose the SVM by volume type extra_specs:

```bash
openstack volume type create arca_tenant_a
openstack volume type set --property arca_storage:svm_name=tenant_a arca_tenant_a
```

Then create volumes with that type.

### `per_project`

Not implemented yet in this repository. (Planned: derive SVM name from `project_id`.)

## Snapshots / clones

- Snapshot is created by copying `volume-<volume_id>` to `snapshot-<snapshot_id>` using sparse-copy (`cp --sparse=always`).
- Volume from snapshot / cloned volume is created by copying the source file to `volume-<new_volume_id>`.
- The driver keeps SVM export mounts to avoid concurrency issues (it does not unmount after each operation).

## QoS

QoS application is best-effort:

- The driver reads volume type extra_specs like `arca_storage:read_iops_sec`.
- If the ARCA REST client is enabled and provides a QoS endpoint, it will try to apply limits; otherwise it is skipped.

## Operational notes

- Ensure NFS client utilities are present on Cinder volume hosts and Nova compute hosts.
- Ensure the NFS export `/exports/<svm>` is reachable and permits the required client CIDRs.
- Monitor disk usage on the underlying export (volumes are sparse but still consume space as written).

## Troubleshooting

- Mount failures: verify `arca_storage_nfs_server`, export path, firewall, and `arca_storage_nfs_mount_options`.
- Permission errors: ensure Cinder service user can create/remove files under the mounted export.
- “Unable to determine NFS export path”: set `arca_storage_nfs_server` or enable `arca_storage_use_api`.
- Snapshot/clone failures: ensure `cp` supports `--sparse=always` (GNU coreutils) and increase `arca_storage_snapshot_copy_timeout` for large volumes.
