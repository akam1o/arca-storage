# OpenStack Manila (NFS) integration

This repository includes a Manila share driver that provisions NFS shares on ARCA Storage via the ARCA REST API.

## Overview

- Each Manila share = ARCA volume (XFS filesystem on LVM thin volume)
- Export path format: `{svm_vip}:/exports/{svm}/{share-volume}`
  - The driver uses `share-<share_id>` as the ARCA volume name
- Snapshots/clones use ARCA backend snapshot/clone APIs
- Access rules are managed via ARCA export ACLs
  - Supported access type: `ip` only
  - Supported access levels: `rw`, `ro`
  - `root_squash` is always enabled for security

## Install / deploy

Make the Python package available on `manila-share` hosts (and any host that imports the driver):

- Install the package from your build artifact (rpm/deb) or `pip install .`
- Optional deps for OpenStack integration: `pip install ".[openstack]"`
- Ensure the module path is importable: `arca_storage.openstack.manila.driver`

## Manila configuration

Example `manila.conf` (multi-backend):

```ini
[DEFAULT]
enabled_share_backends = arca

[arca]
share_driver = arca_storage.openstack.manila.driver.ArcaStorageManilaDriver
share_backend_name = arca_storage
driver_handles_share_servers = False

# ARCA REST API (required)
arca_storage_use_api = true
arca_storage_api_endpoint = http://127.0.0.1:8080
arca_storage_api_timeout = 30
arca_storage_api_retry_count = 3
arca_storage_verify_ssl = true

# Optional authentication
# arca_storage_api_auth_type = token
# arca_storage_api_token = <token>
#
# arca_storage_api_auth_type = basic
# arca_storage_api_username = <username>
# arca_storage_api_password = <password>
#
# (Optional) TLS settings
# arca_storage_api_ca_bundle = /etc/ssl/certs/ca-bundle.crt
# arca_storage_api_client_cert = /path/to/client.crt
# arca_storage_api_client_key = /path/to/client.key

# SVM mapping strategy
arca_storage_svm_strategy = shared
arca_storage_default_svm = manila_default

# (per_project only) Network allocation plugin
# arca_storage_network_plugin_mode = standalone  # or: neutron

# (per_project + standalone) IP/VLAN pools (required when network_plugin_mode=standalone)
# Format: '<ip_cidr>|<start_ip>-<end_ip>:<vlan_id>'
# arca_storage_per_project_ip_pools = 192.168.100.0/24|192.168.100.10-192.168.100.200:100
# arca_storage_per_project_ip_pools = 172.16.0.0/24|172.16.0.100-172.16.0.200:200

# (per_project + neutron) Neutron networks (required when network_plugin_mode=neutron)
# arca_storage_neutron_net_ids = <net-uuid-1>,<net-uuid-2>
# arca_storage_neutron_port_security = false
# arca_storage_neutron_vnic_type = normal

# per_project SVM settings
# arca_storage_per_project_mtu = 1500
# arca_storage_per_project_root_volume_size_gib = 100
```

## SVM mapping strategies

### `shared`

All shares use `arca_storage_default_svm`.

### `manual`

Select the SVM by share type extra_specs:

- `arca_manila:svm_name=<svm_name>`

### `per_project`

Automatically creates one SVM per OpenStack project.

- SVM name: `{arca_storage_svm_prefix}{project_id}` (default prefix: `manila_`)
- VLAN/IP are allocated by `arca_storage_network_plugin_mode`:
  - `standalone`: static pools (`arca_storage_per_project_ip_pools`)
  - `neutron`: Neutron ports on provider VLAN networks (`arca_storage_neutron_net_ids`, `[neutron]` auth)
- SVM garbage collection is not implemented (cleanup is manual)

#### `per_project` + `standalone` (static pools)

- Configure one or more pools via `arca_storage_per_project_ip_pools`
- Projects are assigned to pools using round-robin to reduce collisions
- IPv4 only

#### `per_project` + `neutron` (Neutron ports)

The driver creates one Neutron Port per SVM and uses the port's fixed IP plus the subnet gateway/prefix.

- Networks must be VLAN provider networks (`provider:network_type=vlan`, `provider:segmentation_id` present)
- The allocator auto-selects the first IPv4 subnet with `gateway_ip` (falls back to first subnet)
- Ports use:
  - `device_owner=compute:arca-storage-svm`
  - `device_id=arca-svm-<svm_name>`
  - optional `tags` (best-effort, if Neutron tag extension is available)

Example `manila.conf` additions:

```ini
[arca]
arca_storage_svm_strategy = per_project
arca_storage_network_plugin_mode = neutron
arca_storage_neutron_net_ids = <net-uuid-1>,<net-uuid-2>

[neutron]
auth_type = password
auth_url = https://keystone.example/v3
username = manila
password = <password>
project_name = service
user_domain_name = Default
project_domain_name = Default
region_name = RegionOne
interface = internal
```

## Share types (scheduler / placement)

Create a DHSS=False share type and bind it to the backend:

```bash
openstack share type create arca --snapshot-support True
openstack share type set arca --extra-specs share_backend_name=arca_storage
```

When `arca_storage_svm_strategy=manual`, specify the SVM:

```bash
openstack share type set arca --extra-specs arca_manila:svm_name=tenant_a
```

## QoS (best-effort)

The driver reads these share type extra_specs (optional):

- `arca_manila:read_iops_sec`
- `arca_manila:write_iops_sec`
- `arca_manila:read_bytes_sec`
- `arca_manila:write_bytes_sec`

If the ARCA API supports QoS for volumes, the driver tries to apply limits; otherwise it is skipped.

## Access rules

Only IP-based access rules are supported:

```bash
openstack share access create <share> ip 10.0.0.0/24 --access-level rw
openstack share access delete <share> <access-id>
```

If Manila does not provide incremental access rule diffs, the driver reconciles backend exports against the full desired list as a safety-net.

## Snapshots / shares from snapshots

- Snapshot support: enabled by default (`arca_storage_snapshot_support=true`)
- Create share from snapshot: enabled by default (`arca_storage_create_share_from_snapshot_support=true`)
- Revert to snapshot / mount snapshot: not implemented

For `per_project` strategy, creating a share from snapshot is restricted to the same project as the parent share (to preserve tenant isolation).

## Operational notes

- The ARCA REST API must be reachable from `manila-share`.
- NFS clients (compute nodes/users) must be able to reach the SVM VIP and mount the exported path.
- Plan address space for `per_project` pools: each pool supports `(end_ip - start_ip + 1)` projects; total capacity is the sum of all pools.

## Quick smoke test

Example flow (adjust share type, size, and client CIDR as needed):

```bash
# Create a share
openstack share create NFS 1 --name arca-test --share-type arca

# Get export location and allow a client network
openstack share export location list arca-test
openstack share access create arca-test ip 10.0.0.0/24 --access-level rw

# Snapshot and share-from-snapshot
openstack share snapshot create arca-test --name arca-test-snap
openstack share create NFS 1 --name arca-test-from-snap --share-type arca --snapshot arca-test-snap
```

## Limitations

- Access rules: `ip` only; `user`/`cert` are not supported.
- `per_project` pool allocation supports IPv4 only.
- Snapshot features: revert/mount snapshot are not implemented.
- `per_project` SVM lifecycle: automatic cleanup (GC) is not implemented.
- `per_project` + `neutron`: Neutron port cleanup is best-effort on failed creates, but SVM/port GC is not implemented.
- QoS is best-effort and depends on ARCA API support.

## Troubleshooting

- Driver init fails: verify `arca_storage_api_endpoint` and authentication settings.
- Scheduler places shares but creation fails in driver: confirm the target SVM exists (for `shared`/`manual`) and API is reachable.
- Access rule errors: only `ip` access type is accepted; validate `access_to` is a valid IP/CIDR.
- `per_project` SVM creation fails with conflicts: ensure the pool ranges do not include network/broadcast addresses and that the ranges have enough free IPs.
