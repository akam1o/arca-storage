"""
Configuration loader for Arca Storage.

The primary goal is to avoid hardcoding environment-specific defaults (VG name,
DRBD resource name, parent interface, state directory, etc.) in code.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_BOOTSTRAP_CONFIG_PATH = Path("/etc/arca-storage/storage-bootstrap.conf")
DEFAULT_RUNTIME_CONFIG_PATH = Path("/etc/arca-storage/storage-runtime.conf")


@dataclass(frozen=True)
class ArcaConfig:
    state_dir: Optional[Path] = None
    export_dir: str = "/exports"
    ganesha_config_dir: str = "/etc/ganesha"
    ganesha_protocols: str = "4"  # e.g. "4" or "3,4"
    ganesha_mountd_port: int = 20048
    ganesha_nlm_port: int = 32768
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    vg_name: str = "vg_pool_01"
    thinpool_name: str = "pool"
    parent_if: str = "bond0"
    drbd_resource: str = "r0"
    pacemaker_ra_vendor: str = "local"


def _bootstrap_config_path() -> Path:
    env = os.environ.get("ARCA_BOOTSTRAP_CONFIG_PATH")
    if env:
        return Path(env)
    return DEFAULT_BOOTSTRAP_CONFIG_PATH


def _runtime_config_path() -> Path:
    env = os.environ.get("ARCA_RUNTIME_CONFIG_PATH")
    if env:
        return Path(env)
    return DEFAULT_RUNTIME_CONFIG_PATH


def _read_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    if path.exists():
        parser.read(path, encoding="utf-8")
    return parser


def load_config() -> ArcaConfig:
    """
    Load config from:
    - `ARCA_BOOTSTRAP_CONFIG_PATH` or `/etc/arca-storage/storage-bootstrap.conf`
    - `ARCA_RUNTIME_CONFIG_PATH` or `/etc/arca-storage/storage-runtime.conf`

    Missing files are not an error; defaults are returned.
    """
    bootstrap_parser = _read_ini(_bootstrap_config_path())
    runtime_parser = _read_ini(_runtime_config_path())

    bootstrap_section = bootstrap_parser["storage"] if bootstrap_parser.has_section("storage") else {}
    runtime_section = runtime_parser["storage"] if runtime_parser.has_section("storage") else {}

    def _get(section: object, key: str, default: str) -> str:
        if isinstance(section, dict):
            return str(section.get(key, default)).strip()
        return str(section.get(key, fallback=default)).strip()

    def _get_int(section: object, key: str, default: int) -> int:
        raw = _get(section, key, str(default))
        try:
            return int(raw)
        except Exception:
            return default

    def _parse_protocols(raw: str) -> str:
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        nums: list[int] = []
        for t in tokens:
            try:
                nums.append(int(t))
            except Exception:
                continue
        allowed = {3, 4}
        nums = [n for n in nums if n in allowed]
        if 4 not in nums:
            nums.append(4)
        nums = sorted(set(nums))
        return ",".join(str(n) for n in nums)

    # Runtime config (optional)
    state_dir_raw = _get(runtime_section, "state_dir", "").strip()
    state_dir = Path(state_dir_raw) if state_dir_raw else None

    return ArcaConfig(
        state_dir=state_dir,
        export_dir=_get(runtime_section, "export_dir", "/exports"),
        ganesha_config_dir=_get(runtime_section, "ganesha_config_dir", "/etc/ganesha"),
        ganesha_protocols=_parse_protocols(_get(runtime_section, "ganesha_protocols", "4")),
        ganesha_mountd_port=_get_int(runtime_section, "ganesha_mountd_port", 20048),
        ganesha_nlm_port=_get_int(runtime_section, "ganesha_nlm_port", 32768),
        api_host=_get(runtime_section, "api_host", "127.0.0.1"),
        api_port=_get_int(runtime_section, "api_port", 8080),
        # Bootstrap config (stable)
        vg_name=_get(bootstrap_section, "vg_name", "vg_pool_01"),
        thinpool_name=_get(bootstrap_section, "thinpool_name", "pool"),
        parent_if=_get(bootstrap_section, "parent_if", "bond0"),
        drbd_resource=_get(bootstrap_section, "drbd_resource", "r0"),
        pacemaker_ra_vendor=_get(bootstrap_section, "pacemaker_ra_vendor", "local"),
    )
