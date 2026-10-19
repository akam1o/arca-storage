# Packaging (rpm/deb)

This repository contains packaging scaffolding for:

- EL9/EL10 (RHEL / AlmaLinux / Rocky Linux): rpm
- Debian 12 (bookworm) / 13 (trixie): deb
- Ubuntu 24.04 / 26.04 LTS: deb

CI and release builds cover all four Debian/Ubuntu targets. Each build installs
its package in a fresh container and checks both CLI and API entrypoints before
uploading artifacts.

## Strategy

To avoid relying on distribution Python dependencies (FastAPI/Uvicorn/Typer, etc.), packages install a dedicated virtualenv:

- venv path: `/opt/arca-storage/venv`
- wrappers:
  - `/usr/bin/arca` → venv `arca`
  - `/usr/bin/arca-storage-api` → venv `arca-storage-api`

System resources are installed to standard locations:

- config (conffile): `/etc/arca-storage/config.toml`
- optional API env file (conffile): `/etc/arca-storage/api.env`
- derived systemd env file (conffile, refresh with `arca bootstrap render-env`): `/etc/arca-storage/arca-storage.env`
- systemd units: `arca-storage-api.service`, `nfs-ganesha@.service`, `nfs-ganesha-host@.service`
- Pacemaker RA: `/usr/lib/ocf/resource.d/local/NetnsVlan`

## Runtime services

The API manages host LVM volumes and mounts that NFS-Ganesha must see. Its unit
runs in the host mount namespace and needs write access to LVM's metadata backup
directories under `/etc/lvm`. Mount-namespace sandboxing such as `PrivateTmp`,
`ProtectSystem`, `ProtectHome`, `ReadWritePaths`, or `ProtectKernelLogs` prevents
these storage operations from working correctly. The unit retains its capability
bounding set, `NoNewPrivileges`, and the other independent hardening settings.

The host-namespace Ganesha unit requires `rpcbind.service`, including for NFSv4-only
exports, because Ganesha registers its RQUOTA service at startup. Install rpcbind
on package-managed hosts; the Ansible NFS-Ganesha role includes it by default.

## Wheelhouse requirement

The build uses an offline wheelhouse containing runtime dependencies.

Arca Storage requires Python 3.10 or newer. Package interpreters are:

| Distribution | Python |
| --- | --- |
| EL9 | 3.11 |
| EL10 | 3.12 |
| Debian 12 (bookworm) | 3.11 |
| Debian 13 (trixie) | 3.13 |
| Ubuntu 24.04 | 3.12 |
| Ubuntu 26.04 | 3.14 |

Generate the wheelhouse with the same interpreter that will build the package,
typically inside the target container/CI job.

Create wheelhouse:

```bash
bash ./packaging/vendor-wheels.sh
```

On EL9, install `python3.11` and `python3.11-pip`, then select Python 3.11
explicitly because the system `python3` is still Python 3.9:

```bash
sudo dnf install -y python3.11 python3.11-pip
ARCA_BUILD_PYTHON=python3.11 bash ./packaging/vendor-wheels.sh
```

It produces:

- `packaging/wheelhouse/*.whl`

## Build (local)

### Debian/Ubuntu (deb)

```bash
bash ./packaging/build-deb.sh
```

### EL9/EL10 (rpm)

```bash
bash ./packaging/build-rpm.sh
```

## Notes

- These scripts are intended for CI/container builds. Host tooling requirements vary.
- If `packaging/wheelhouse/` is missing, the build scripts will run `./packaging/vendor-wheels.sh` automatically.
  The RPM build selects Python 3.11 on EL9 and system `python3` on EL10.
- After changing source or `ARCA_VERSION`, regenerate the wheelhouse with the same
  `ARCA_VERSION` used for the package build. Existing wheelhouses are reused for
  offline builds; the embedded Arca wheel must match the requested package
  version. A version mismatch now fails the build instead of silently installing
  older code under a new package version. Source changes that retain the same
  version still require explicitly regenerating the wheelhouse.
- Package versions are derived from the release tag (e.g. `v0.2.2`). Override with `ARCA_VERSION=0.2.2`.
- deb builds append a distro suffix (e.g. `0.2.2-1.debian13`, `0.2.2-1.ubuntu26.04`) so Debian/Ubuntu artifacts don't overwrite each other.
- Packages do not auto-enable services by default. Configure the API token
  before enabling the service:

  ```bash
  API_TOKEN="$(openssl rand -hex 32)"
  sudo install -m 0600 /dev/null /etc/arca-storage/api.env
  printf 'ARCA_API_TOKEN=%s\n' "$API_TOKEN" | sudo tee /etc/arca-storage/api.env >/dev/null
  unset API_TOKEN
  sudo systemctl enable --now arca-storage-api
  ```
