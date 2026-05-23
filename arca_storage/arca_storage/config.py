"""Unified TOML configuration for Arca Storage."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Optional, TypedDict, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from arca_storage.cli.lib.validators import normalize_nfs_client_cidr


DEFAULT_CONFIG_PATH = Path("/etc/arca-storage/config.toml")
_PATH_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SYSTEMD_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_SYSTEMD_ENV_SAFE_VALUE_RE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")
_MAX_TIMEOUT_SECONDS = 24 * 60 * 60


class ReconcilerConfig(TypedDict):
    """Settings contract shared with reconcilers."""

    vg_name: str
    thinpool_name: str
    parent_if: str
    export_dir: str
    drbd_resource: str


def _validate_absolute_posix_path(value: str, *, field_name: str) -> str:
    raw = str(value).strip()
    if not raw:
        raise ValueError(f"{field_name} must not be empty")
    if "\x00" in raw:
        raise ValueError(f"{field_name} must not contain NUL bytes")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError(f"{field_name} must not contain control characters")

    path = PurePosixPath(raw)
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be an absolute POSIX path")

    segments = [part for part in raw.split("/") if part]
    if not segments:
        raise ValueError(f"{field_name} must not be the filesystem root")
    if any(part in {".", ".."} for part in segments):
        raise ValueError(f"{field_name} must not contain relative path segments")

    return str(path)


def validate_path_component(value: str, *, field_name: str) -> str:
    raw = str(value).strip()
    if not raw:
        raise ValueError(f"{field_name} must not be empty")
    if raw in {".", ".."} or "/" in raw or "\\" in raw:
        raise ValueError(f"{field_name} must be a single safe path component")
    if not _PATH_COMPONENT_RE.fullmatch(raw):
        raise ValueError(
            f"{field_name} must start with an alphanumeric character and contain only "
            "alphanumeric characters, dots, underscores, or hyphens"
        )
    return raw


def _systemd_env_assignment(name: str, value: str) -> str:
    if not _SYSTEMD_ENV_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid systemd environment variable name: {name!r}")

    raw = str(value)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError(f"{name} must not contain control characters")
    if _SYSTEMD_ENV_SAFE_VALUE_RE.fullmatch(raw):
        rendered = raw
    else:
        rendered = '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name}={rendered}"


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vg_name: str = "vg_pool_01"
    thinpool_name: str = "pool"

    @field_validator("vg_name", "thinpool_name")
    @classmethod
    def validate_lvm_names(cls, value: str, info: ValidationInfo) -> str:
        return validate_path_component(value, field_name=f"storage.{info.field_name}")


class NetworkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_interface: str = "bond0"
    default_nfs_versions: list[str] = Field(default_factory=lambda: ["4"])

    @field_validator("parent_interface")
    @classmethod
    def validate_parent_interface(cls, value: str) -> str:
        return validate_path_component(value, field_name="network.parent_interface")


class ClusterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pacemaker_cluster_name: str = "arca-cluster"
    enable_stonith: bool = True
    drbd_resource: str = "r0"
    pacemaker_ra_vendor: str = "local"

    @field_validator("drbd_resource")
    @classmethod
    def validate_drbd_resource(cls, value: str) -> str:
        return validate_path_component(value, field_name="cluster.drbd_resource")

    @field_validator("pacemaker_ra_vendor")
    @classmethod
    def validate_pacemaker_ra_vendor(cls, value: str) -> str:
        return validate_path_component(value, field_name="cluster.pacemaker_ra_vendor")


class APIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bind: str = "127.0.0.1"
    port: int = 8080
    ssl_certfile: Optional[str] = None
    ssl_keyfile: Optional[str] = None

    @field_validator("ssl_certfile", "ssl_keyfile", mode="before")
    @classmethod
    def validate_tls_paths(
        cls, value: Optional[str], info: ValidationInfo
    ) -> Optional[str]:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        return _validate_absolute_posix_path(raw, field_name=f"api.{info.field_name}")

    @model_validator(mode="after")
    def validate_tls_pair(self) -> "APIConfig":
        if bool(self.ssl_certfile) != bool(self.ssl_keyfile):
            raise ValueError(
                "api.ssl_certfile and api.ssl_keyfile must be provided together"
            )
        return self


class TimeoutConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subprocess_default: int = Field(default=30, gt=0, le=_MAX_TIMEOUT_SECONDS)
    pacemaker_operation: int = Field(default=60, gt=0, le=_MAX_TIMEOUT_SECONDS)
    nfs_mount: int = Field(default=15, gt=0, le=_MAX_TIMEOUT_SECONDS)


class StateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db_path: str = "/var/lib/arca-storage/state.db"
    runtime_dir: str = "/var/lib/arca-storage"

    @field_validator("db_path", "runtime_dir")
    @classmethod
    def validate_paths(cls, value: str, info: ValidationInfo) -> str:
        return _validate_absolute_posix_path(
            value, field_name=f"state.{info.field_name}"
        )


class GaneshaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_dir: str = "/etc/ganesha"
    export_dir: str = "/exports"
    protocols: list[int] = Field(default_factory=lambda: [4])
    mountd_port: int = 20048
    nlm_port: int = 32768

    @field_validator("config_dir", "export_dir")
    @classmethod
    def validate_paths(cls, value: str, info: ValidationInfo) -> str:
        return _validate_absolute_posix_path(
            value, field_name=f"ganesha.{info.field_name}"
        )

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
                normalized = normalize_nfs_client_cidr(str(raw).strip())
            except Exception as e:
                raise ValueError(f"invalid CSI client CIDR {raw!r}: {e}") from e
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

    def to_reconciler_config(self) -> ReconcilerConfig:
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
            _systemd_env_assignment("ARCA_GANESHA_CONFIG_DIR", self.ganesha.config_dir),
        ]
        return "\n".join(lines) + "\n"


def _load_toml(path: Path) -> dict:
    """Load TOML using tomllib (Python 3.11+) or tomli."""
    try:
        import tomllib  # type: ignore[import-not-found]  # Python 3.11+
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]
    with path.open("rb") as f:
        return tomllib.load(f)


def load_settings(
    path: Optional[Union[Path, str]] = None, *, require_file: bool = True
) -> ArcaSettings:
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
