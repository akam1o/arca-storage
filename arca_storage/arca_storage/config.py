"""Unified TOML configuration for Arca Storage."""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


DEFAULT_CONFIG_PATH = Path("/etc/arca-storage/config.toml")


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vg_name: str = "vg_pool_01"
    thinpool_name: str = "pool"


class NetworkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_interface: str = "bond0"
    default_nfs_versions: list[str] = Field(default_factory=lambda: ["4"])


class ClusterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pacemaker_cluster_name: str = "arca-cluster"
    enable_stonith: bool = True
    drbd_resource: str = "r0"
    pacemaker_ra_vendor: str = "local"


class APIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bind: str = "127.0.0.1"
    port: int = 8080


class TimeoutConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subprocess_default: int = 30
    pacemaker_operation: int = 60
    nfs_mount: int = 15


class StateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db_path: str = "/var/lib/arca-storage/state.db"
    runtime_dir: str = "/var/lib/arca-storage"


class GaneshaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_dir: str = "/etc/ganesha"
    export_dir: str = "/exports"
    protocols: list[int] = Field(default_factory=lambda: [4])
    mountd_port: int = 20048
    nlm_port: int = 32768

    @field_validator("protocols")
    @classmethod
    def validate_protocols(cls, value: list[int]) -> list[int]:
        protocols = sorted(set(value))
        if not protocols:
            raise ValueError("ganesha.protocols must not be empty")
        unsupported = [p for p in protocols if p not in (3, 4)]
        if unsupported:
            raise ValueError(f"unsupported NFS protocol versions: {unsupported}")
        return protocols


class CSIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_cidrs: list[str] = Field(default_factory=list)
    root_squash: bool = True

    @field_validator("client_cidrs")
    @classmethod
    def validate_client_cidrs(cls, value: list[str]) -> list[str]:
        cidrs: list[str] = []
        seen: set[str] = set()
        for raw in value:
            try:
                network = ipaddress.ip_network(str(raw).strip(), strict=False)
            except Exception as e:
                raise ValueError(f"invalid CSI client CIDR {raw!r}: {e}") from e
            if network.version != 4:
                raise ValueError(f"CSI client CIDR must be IPv4: {raw!r}")
            if network.prefixlen == 0:
                raise ValueError("CSI client CIDRs must not include the IPv4 default route")
            normalized = str(network)
            if normalized not in seen:
                seen.add(normalized)
                cidrs.append(normalized)
        return cidrs


class ArcaSettings(BaseModel):
    """Top-level validated configuration."""

    model_config = ConfigDict(extra="forbid")

    storage: StorageConfig = Field(default_factory=StorageConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    ganesha: GaneshaConfig = Field(default_factory=GaneshaConfig)
    csi: CSIConfig = Field(default_factory=CSIConfig)

    def to_reconciler_config(self) -> dict:
        """Flatten settings into the dict reconcilers expect."""
        return {
            "vg_name": self.storage.vg_name,
            "thinpool_name": self.storage.thinpool_name,
            "parent_if": self.network.parent_interface,
            "export_dir": self.ganesha.export_dir,
            "drbd_resource": self.cluster.drbd_resource,
        }

    def to_systemd_env(self) -> str:
        """Render derived environment variables consumed by systemd units."""
        lines = [
            "# Managed by arca bootstrap (derived from /etc/arca-storage/config.toml)",
            f"ARCA_GANESHA_CONFIG_DIR={self.ganesha.config_dir}",
        ]
        return "\n".join(lines) + "\n"


def _load_toml(path: Path) -> dict:
    """Load TOML using tomllib (Python 3.11+) or tomli."""
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]
    with path.open("rb") as f:
        return tomllib.load(f)


def load_settings(path: Optional[Union[Path, str]] = None, *, require_file: bool = True) -> ArcaSettings:
    """Load and validate configuration.

    Resolution order:
    1. Explicit path argument
    2. ``ARCA_CONFIG_PATH`` env var
    3. ``/etc/arca-storage/config.toml``

    The config file is required by default so operational commands never fall
    back to built-in values silently. Tests may pass ``require_file=False`` to
    exercise the default schema without an on-disk config.
    """
    explicit = path is not None
    if path is None:
        env = os.environ.get("ARCA_CONFIG_PATH")
        if env:
            path = Path(env)
            explicit = True
        else:
            path = DEFAULT_CONFIG_PATH

    path = Path(path)
    if path.exists():
        raw = _load_toml(path)
        return ArcaSettings.model_validate(raw)

    if explicit or require_file:
        raise FileNotFoundError(f"Arca config file not found: {path}")

    # Test/development fallback: pure defaults (still validates).
    return ArcaSettings()
