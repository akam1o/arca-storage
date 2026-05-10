"""
NFS-Ganesha configuration management functions.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from jinja2 import Template

from arca_storage.cli.lib.state import get_state_dir
from arca_storage.config import ArcaSettings, load_settings

TEMPLATE_VERSION = "1.1.0"
_DEFAULT_COMMAND_TIMEOUT_SECONDS = 30
_GANESHA_SEC_TYPES = {"sys", "krb5", "krb5i", "krb5p"}
_GANESHA_ACCESS_TYPES = {"RW", "RO"}
_GANESHA_SQUASH_TYPES = {"Root_Squash", "No_Root_Squash"}


def _template_path() -> Path:
    # arca_storage/cli/lib/ganesha.py -> arca_storage/templates/ganesha.conf.j2
    return Path(__file__).resolve().parents[2] / "templates" / "ganesha.conf.j2"

def _config_snapshot_dir(settings: Optional[ArcaSettings] = None) -> Path:
    # Keep snapshots under the same persistent state directory as exports.*.json.
    if settings is not None:
        return Path(settings.state.runtime_dir) / "config"
    return get_state_dir() / "config"


def _snapshot_path(svm_name: str, config_version: str, snapshot_dir: Optional[Path] = None) -> Path:
    return (snapshot_dir or _config_snapshot_dir()) / f"ganesha.{svm_name}.{config_version}.conf"

def _snapshot_meta_path(svm_name: str, config_version: str, snapshot_dir: Optional[Path] = None) -> Path:
    return (snapshot_dir or _config_snapshot_dir()) / f"ganesha.{svm_name}.{config_version}.json"


def _render_sectype(value: object) -> str:
    # Ganesha expects SecType as tokens (e.g. "sys" or "sys, krb5").
    if isinstance(value, list):
        raw_tokens = [str(v).strip().lower() for v in value if str(v).strip()]
    else:
        raw_tokens = [token.strip().lower() for token in str(value).split(",") if token.strip()]

    tokens = raw_tokens or ["sys"]
    unsupported = [token for token in tokens if token not in _GANESHA_SEC_TYPES]
    if unsupported:
        raise ValueError(f"Unsupported Ganesha SecType value(s): {unsupported}")
    return ", ".join(tokens)


def _ganesha_quoted_string(value: object, field: str) -> str:
    if value is None:
        raise ValueError(f"Missing Ganesha {field}")
    raw = str(value)
    if not raw:
        raise ValueError(f"Missing Ganesha {field}")
    invalid = [ch for ch in raw if ch in {'"', "\\"} or ord(ch) < 0x20 or ord(ch) == 0x7F]
    if invalid:
        raise ValueError(f"Unsafe Ganesha {field}: contains unsupported characters")
    return raw


def _ganesha_token(value: object, field: str, allowed: set[str]) -> str:
    raw = str(value).strip()
    if raw not in allowed:
        raise ValueError(f"Unsupported Ganesha {field}: {raw!r}")
    return raw


def _stable_config_version(
    *,
    svm_name: str,
    bind_addr: Optional[str],
    protocols: str,
    mountd_port: int,
    nlm_port: int,
    exports: Sequence[Dict],
) -> str:
    payload = {
        "svm": svm_name,
        "bind_addr": bind_addr,
        "protocols": protocols,
        "mountd_port": mountd_port,
        "nlm_port": nlm_port,
        "exports": [
            {
                "export_id": e.get("export_id"),
                "path": e.get("path"),
                "pseudo": e.get("pseudo"),
                "access": e.get("access"),
                "squash": e.get("squash"),
                "sec": e.get("sec"),
                "client": e.get("client"),
            }
            for e in exports
        ],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return digest[:12]


def _write_if_changed(path: Path, content: str) -> None:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                if f.read() == content:
                    return
        except Exception:
            # If we can't read, fall back to writing.
            pass

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_parent_dir(path.parent)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def _fsync_parent_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_if_changed(path: Path, data: object) -> None:
    content = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    _write_if_changed(path, content)


def render_config(
    svm_name: str,
    exports: List[Dict],
    *,
    bind_addr: Optional[str] = None,
    settings: Optional[ArcaSettings] = None,
) -> str:
    """
    Render ganesha.conf configuration file.
    
    Args:
        svm_name: SVM name
        exports: List of export dictionaries
        
    Returns:
        Path to the generated config file
    """
    cfg = settings or load_settings()
    config_dir = Path(cfg.ganesha.config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    
    config_path = config_dir / f"ganesha.{svm_name}.conf"

    # Render template from templates/ganesha.conf.j2 (single source of truth).
    template = Template(_template_path().read_text(encoding="utf-8"))
    protocol_tokens = [str(p) for p in cfg.ganesha.protocols]
    # Render as "3, 4" to match ganesha.conf conventions.
    protocols = ", ".join(protocol_tokens) if protocol_tokens else "4"
    enable_v3 = 3 in cfg.ganesha.protocols

    # Stable ordering for deterministic output.
    exports_sorted = sorted(
        list(exports),
        key=lambda e: (
            int(e.get("export_id") or 0),
            str(e.get("path") or ""),
            str(e.get("client") or ""),
        ),
    )

    exports_render: List[Dict] = []
    for e in exports_sorted:
        sec = e.get("sec", ["sys"])
        exports_render.append(
            {
                **e,
                "path": _ganesha_quoted_string(e.get("path"), "Path"),
                "pseudo": _ganesha_quoted_string(e.get("pseudo"), "Pseudo"),
                "client": _ganesha_quoted_string(e.get("client"), "Clients"),
                "access": _ganesha_token(e.get("access", "RW"), "Access_Type", _GANESHA_ACCESS_TYPES),
                "squash": _ganesha_token(e.get("squash", "Root_Squash"), "Squash", _GANESHA_SQUASH_TYPES),
                "sec_render": _render_sectype(sec),
            }
        )

    config_version = _stable_config_version(
        svm_name=svm_name,
        bind_addr=bind_addr,
        protocols=protocols,
        mountd_port=cfg.ganesha.mountd_port,
        nlm_port=cfg.ganesha.nlm_port,
        exports=exports_render,
    )
    meta = {
        "template_version": TEMPLATE_VERSION,
        "config_version": config_version,
        "bind_addr": bind_addr,
        "protocols": protocols,
        "mountd_port": cfg.ganesha.mountd_port,
        "nlm_port": cfg.ganesha.nlm_port if enable_v3 else None,
        "exports": [
            {
                "export_id": e.get("export_id"),
                "path": e.get("path"),
                "pseudo": e.get("pseudo"),
                "access": e.get("access"),
                "squash": e.get("squash"),
                "sec": e.get("sec"),
                "client": e.get("client"),
            }
            for e in exports_sorted
        ],
    }
    config_content = template.render(
        template_version=TEMPLATE_VERSION,
        config_version=config_version,
        exports=exports_render,
        bind_addr=bind_addr,
        protocols=protocols,
        enable_v3=enable_v3,
        mountd_port=cfg.ganesha.mountd_port,
        nlm_port=cfg.ganesha.nlm_port,
    )

    # Save snapshots for rollback purposes.
    snapshot_dir = _config_snapshot_dir(cfg)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    _write_if_changed(_snapshot_path(svm_name, config_version, snapshot_dir), config_content)
    _write_if_changed(snapshot_dir / f"ganesha.{svm_name}.latest.conf", config_content)
    _write_json_if_changed(_snapshot_meta_path(svm_name, config_version, snapshot_dir), meta)
    _write_json_if_changed(snapshot_dir / f"ganesha.{svm_name}.latest.json", meta)

    _write_if_changed(config_path, config_content)
    
    return str(config_path)


def reload(svm_name: str, *, host_network: bool = False) -> None:
    """
    Reload NFS-Ganesha service for an SVM.
    
    Args:
        svm_name: SVM name
        
    Raises:
        RuntimeError: If reload fails
    """
    import subprocess

    # Reload using systemctl
    unit = "nfs-ganesha-host" if host_network else "nfs-ganesha"
    result = subprocess.run(
        ["systemctl", "reload", f"{unit}@{svm_name}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=_DEFAULT_COMMAND_TIMEOUT_SECONDS,
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to reload NFS-Ganesha: {result.stderr}")


def add_export(
    svm_name: str,
    volume_name: str,
    client: str,
    access: str = "rw",
    root_squash: bool = True,
    sec: Optional[List[str]] = None,
) -> None:
    """
    Add an export to the ganesha configuration.
    
    Args:
        svm_name: SVM name
        volume_name: Volume name
        client: Client CIDR
        access: Access type (rw or ro)
        root_squash: Enable root squash
        
    Raises:
        RuntimeError: If adding export fails
    """
    # Load existing exports
    exports = _load_exports(svm_name)
    
    # Generate export ID (simple increment)
    export_id = max([e.get("export_id", 0) for e in exports], default=0) + 1
    
    # Create export entry
    cfg = load_settings()
    export_dir = cfg.ganesha.export_dir.rstrip("/")
    export_entry = {
        "export_id": export_id,
        "path": f"{export_dir}/{svm_name}/{volume_name}",
        "pseudo": f"{export_dir}/{svm_name}/{volume_name}",
        "access": access.upper(),
        "squash": "Root_Squash" if root_squash else "No_Root_Squash",
        "sec": sec or ["sys"],
        "client": client,
    }
    
    exports.append(export_entry)
    
    # Save exports and regenerate config
    _save_exports(svm_name, exports)
    render_config(svm_name, exports)
    
    # Reload service
    reload(svm_name)


def remove_export(svm_name: str, volume_name: str, client: str) -> None:
    """
    Remove an export from the ganesha configuration.
    
    Args:
        svm_name: SVM name
        volume_name: Volume name
        client: Client CIDR
        
    Raises:
        RuntimeError: If removing export fails
    """
    # Load existing exports
    exports = _load_exports(svm_name)
    
    # Remove matching export
    cfg = load_settings()
    export_dir = cfg.ganesha.export_dir.rstrip("/")
    exports = [
        e
        for e in exports
        if not (e.get("path") == f"{export_dir}/{svm_name}/{volume_name}" and e.get("client") == client)
    ]
    
    # Save exports and regenerate config
    _save_exports(svm_name, exports)
    render_config(svm_name, exports)
    
    # Reload service
    reload(svm_name)


def _load_exports(svm_name: str) -> List[Dict]:
    """Load exports from state file."""
    state_dir = get_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    
    state_file = state_dir / f"exports.{svm_name}.json"
    
    if state_file.exists():
        with open(state_file, "r") as f:
            return json.load(f)
    
    return []


def _save_exports(svm_name: str, exports: List[Dict]) -> None:
    """Save exports to state file."""
    state_dir = get_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    
    state_file = state_dir / f"exports.{svm_name}.json"

    tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{state_file.name}.", dir=str(state_file.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(exports, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, state_file)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def list_exports(svm_name: Optional[str] = None, volume_name: Optional[str] = None) -> List[Dict]:
    """
    List exports from state files.
    """
    state_dir = get_state_dir()
    if not state_dir.exists():
        return []

    exports: List[Dict] = []
    if svm_name:
        per_svm = _load_exports(svm_name)
        for e in per_svm:
            exports.append({"svm": svm_name, "volume": _volume_from_path(e.get("path", "")), **e})
    else:
        for path in state_dir.glob("exports.*.json"):
            name = path.name[len("exports.") : -len(".json")]
            per_svm = _load_exports(name)
            for e in per_svm:
                exports.append({"svm": name, "volume": _volume_from_path(e.get("path", "")), **e})

    if volume_name:
        exports = [e for e in exports if e.get("volume") == volume_name]
    return exports


def sync(svm_name: str) -> str:
    """
    Re-render ganesha.conf from current state and reload the service.

    Useful after changing runtime configuration (e.g., enabling NFSv3).
    """
    exports = _load_exports(svm_name)
    path = render_config(svm_name, exports)
    reload(svm_name)
    return path


def list_config_snapshots(svm_name: str) -> List[Dict]:
    """
    List saved ganesha.conf snapshots for a given SVM.
    """
    snapshot_dir = _config_snapshot_dir()
    if not snapshot_dir.exists():
        return []

    results: List[Dict] = []
    prefix = f"ganesha.{svm_name}."
    suffix = ".conf"
    for p in snapshot_dir.glob(f"{prefix}*{suffix}"):
        if p.name == f"ganesha.{svm_name}.latest.conf":
            continue
        version = p.name[len(prefix) : -len(suffix)]
        try:
            mtime = p.stat().st_mtime
        except Exception:
            mtime = 0.0
        results.append({"config_version": version, "path": str(p), "mtime": mtime})

    results.sort(key=lambda x: float(x.get("mtime") or 0.0), reverse=True)
    return results


def rollback_config(svm_name: str, config_version: str, *, host_network: bool = False) -> str:
    """
    Restore ganesha.<svm>.conf from a saved snapshot and reload the service.

    Args:
        svm_name: SVM name
        config_version: Snapshot version (or "latest")
    """
    cfg = load_settings()
    config_dir = Path(cfg.ganesha.config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"ganesha.{svm_name}.conf"

    snapshot_dir = _config_snapshot_dir()
    if config_version == "latest":
        snap = snapshot_dir / f"ganesha.{svm_name}.latest.conf"
    else:
        snap = _snapshot_path(svm_name, config_version)

    if not snap.exists():
        raise FileNotFoundError(f"Snapshot not found: {snap}")

    content = snap.read_text(encoding="utf-8")
    _write_if_changed(config_path, content)
    reload(svm_name, host_network=host_network)
    return str(config_path)


def read_config_snapshot_meta(svm_name: str, config_version: str) -> Dict:
    snapshot_dir = _config_snapshot_dir()
    if config_version == "latest":
        path = snapshot_dir / f"ganesha.{svm_name}.latest.json"
    else:
        path = _snapshot_meta_path(svm_name, config_version)
    if not path.exists():
        raise FileNotFoundError(f"Snapshot metadata not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _volume_from_path(path: str) -> str:
    # Expected: <export_dir>/<svm>/<volume> (base dir is configurable)
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""
    return parts[-1]
