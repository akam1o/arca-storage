"""
Local persistent state store for Arca Storage.

This module is used by both the CLI and API service layer to provide basic
list/get semantics without requiring Pacemaker introspection.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from arca_storage.cli.lib.config import load_config


def get_state_dir() -> Path:
    """
    Resolve the directory used for persistent state.

    Priority:
    1) `ARCA_STATE_DIR` env var, if set
    2) `/var/lib/arca` if writable
    3) `$XDG_STATE_HOME/arca` or `~/.local/state/arca` as fallback
    """
    env = os.environ.get("ARCA_STATE_DIR")
    if env:
        return Path(env)

    cfg = load_config()
    if cfg.state_dir:
        return cfg.state_dir

    candidates: list[Path] = [Path("/var/lib/arca")]
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        candidates.append(Path(xdg_state_home) / "arca")
    else:
        candidates.append(Path.home() / ".local" / "state" / "arca")

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return candidate
        except Exception:
            continue

    return Path(".arca-state")


def _state_dir() -> Path:
    return get_state_dir()


def _svms_file() -> Path:
    return _state_dir() / "svms.json"


def _volumes_file() -> Path:
    return _state_dir() / "volumes.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=False, sort_keys=True)
            file.write("\n")
        os.replace(tmp_path, path)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def list_svms(name: Optional[str] = None) -> List[Dict[str, Any]]:
    svms = _load_json(_svms_file(), {"items": []}).get("items", [])
    if name:
        svms = [s for s in svms if s.get("name") == name]
    return svms


def upsert_svm(svm: Dict[str, Any]) -> None:
    data = _load_json(_svms_file(), {"items": []})
    items = data.get("items", [])
    items = [i for i in items if i.get("name") != svm.get("name")]
    if "created_at" not in svm:
        svm["created_at"] = _utc_now_iso()
    items.append(svm)
    data["items"] = sorted(items, key=lambda x: x.get("name", ""))
    _atomic_write_json(_svms_file(), data)


def delete_svm(name: str) -> bool:
    data = _load_json(_svms_file(), {"items": []})
    items = data.get("items", [])
    new_items = [i for i in items if i.get("name") != name]
    if len(new_items) == len(items):
        return False
    data["items"] = new_items
    _atomic_write_json(_svms_file(), data)
    return True


def list_volumes(svm: Optional[str] = None, name: Optional[str] = None) -> List[Dict[str, Any]]:
    volumes = _load_json(_volumes_file(), {"items": []}).get("items", [])
    if svm:
        volumes = [v for v in volumes if v.get("svm") == svm]
    if name:
        volumes = [v for v in volumes if v.get("name") == name]
    return volumes


def upsert_volume(volume: Dict[str, Any]) -> None:
    data = _load_json(_volumes_file(), {"items": []})
    items = data.get("items", [])
    items = [i for i in items if not (i.get("svm") == volume.get("svm") and i.get("name") == volume.get("name"))]
    if "created_at" not in volume:
        volume["created_at"] = _utc_now_iso()
    items.append(volume)
    data["items"] = sorted(items, key=lambda x: (x.get("svm", ""), x.get("name", "")))
    _atomic_write_json(_volumes_file(), data)


def delete_volume(svm: str, name: str) -> bool:
    data = _load_json(_volumes_file(), {"items": []})
    items = data.get("items", [])
    new_items = [i for i in items if not (i.get("svm") == svm and i.get("name") == name)]
    if len(new_items) == len(items):
        return False
    data["items"] = new_items
    _atomic_write_json(_volumes_file(), data)
    return True
