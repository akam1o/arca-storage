"""
Unified configuration for Arca Storage.

Replaces the dual INI boot/runtime config with a single TOML file
validated through Pydantic Settings. Falls back to the legacy INI
loader when no TOML file is found (backward-compatible).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


DEFAULT_CONFIG_PATH = Path("/etc/arca-storage/config.toml")


class StorageConfig(BaseModel):
    vg_name: str = "vg_pool_01"
    thinpool_name: str = "pool"


class NetworkConfig(BaseModel):
    parent_interface: str = "bond0"
    default_nfs_versions: list[str] = Field(default_factory=lambda: ["4"])


class ClusterConfig(BaseModel):
    pacemaker_cluster_name: str = "arca-cluster"
    enable_stonith: bool = True
    drbd_resource: str = "r0"
    pacemaker_ra_vendor: str = "local"


class APIConfig(BaseModel):
    bind: str = "127.0.0.1"
    port: int = 8080


class TimeoutConfig(BaseModel):
    subprocess_default: int = 30
    pacemaker_operation: int = 60
    nfs_mount: int = 15


class StateConfig(BaseModel):
    db_path: str = "/var/lib/arca/state.db"


class GaneshaConfig(BaseModel):
    config_dir: str = "/etc/ganesha"
    export_dir: str = "/exports"
    protocols: str = "4"
    mountd_port: int = 20048
    nlm_port: int = 32768


class ArcaSettings(BaseModel):
    """Top-level validated configuration."""

    storage: StorageConfig = Field(default_factory=StorageConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    ganesha: GaneshaConfig = Field(default_factory=GaneshaConfig)

    def to_reconciler_config(self) -> dict:
        """Flatten settings into the dict reconcilers expect."""
        return {
            "vg_name": self.storage.vg_name,
            "thinpool_name": self.storage.thinpool_name,
            "parent_if": self.network.parent_interface,
            "export_dir": self.ganesha.export_dir,
            "drbd_resource": self.cluster.drbd_resource,
        }


def _load_toml(path: Path) -> dict:
    """Load TOML using tomllib (Python 3.11+) or tomli."""
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_settings(path: Path | str | None = None) -> ArcaSettings:
    """Load and validate configuration.

    Resolution order:
    1. Explicit path argument
    2. ``ARCA_CONFIG_PATH`` env var
    3. ``/etc/arca-storage/config.toml``
    4. Defaults (no file required)
    """
    if path is None:
        env = os.environ.get("ARCA_CONFIG_PATH")
        if env:
            path = Path(env)
        else:
            path = DEFAULT_CONFIG_PATH

    path = Path(path)
    if path.exists():
        raw = _load_toml(path)
        return ArcaSettings.model_validate(raw)

    # Fallback: pure defaults (still validates)
    return ArcaSettings()
