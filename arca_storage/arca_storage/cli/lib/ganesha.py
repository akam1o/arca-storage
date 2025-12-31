"""
NFS-Ganesha configuration management functions.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from jinja2 import Template

from arca_storage.cli.lib.config import load_config
from arca_storage.cli.lib.state import get_state_dir

# Template for ganesha.conf
GANESHA_CONF_TEMPLATE = """# Managed by arca
# Template version: {{ template_version }}
# Config version: {{ config_version }}

NFS_CORE_PARAM {
    Protocols = {{ protocols }};
    NFS_Port = 2049;
    MNT_Port = {{ mountd_port }};
{% if enable_v3 %}
    NLM_Port = {{ nlm_port }};
{% endif %}
}

EXPORT_DEFAULTS {
    Access_Type = RW;
    Squash = Root_Squash;
}

{% for exp in exports %}
EXPORT {
    Export_Id = {{ exp.export_id }};
    Path = "{{ exp.path }}";
    Pseudo = "{{ exp.pseudo }}";
    Protocols = {{ protocols }};
    Access_Type = {{ exp.access }};
    Squash = {{ exp.squash }};
    SecType = {{ exp.sec_render }};
    CLIENT {
        Clients = "{{ exp.client }}";
    }
    FSAL {
        Name = VFS;
    }
}
{% endfor %}
"""


def render_config(svm_name: str, exports: List[Dict]) -> str:
    """
    Render ganesha.conf configuration file.
    
    Args:
        svm_name: SVM name
        exports: List of export dictionaries
        
    Returns:
        Path to the generated config file
    """
    cfg = load_config()
    config_dir = Path(cfg.ganesha_config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    
    config_path = config_dir / f"ganesha.{svm_name}.conf"
    
    # Generate config version (timestamp)
    import datetime
    config_version = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    template_version = "1.0.0"
    
    # Render template
    template = Template(GANESHA_CONF_TEMPLATE)
    protocol_tokens = [p.strip() for p in cfg.ganesha_protocols.split(",") if p.strip()]
    # Render as "3, 4" to match ganesha.conf conventions.
    protocols = ", ".join(protocol_tokens) if protocol_tokens else "4"
    enable_v3 = "3" in protocol_tokens

    def _sec_render(value: object) -> str:
        # Ganesha expects SecType as tokens (e.g. "sys" or "sys, krb5").
        if isinstance(value, list):
            tokens = [str(v).strip() for v in value if str(v).strip()]
            return ", ".join(tokens) if tokens else "sys"
        raw = str(value).strip()
        return raw or "sys"

    exports_render: List[Dict] = []
    for e in exports:
        sec = e.get("sec", ["sys"])
        exports_render.append({**e, "sec_render": _sec_render(sec)})
    config_content = template.render(
        template_version=template_version,
        config_version=config_version,
        exports=exports_render,
        protocols=protocols,
        enable_v3=enable_v3,
        mountd_port=cfg.ganesha_mountd_port,
        nlm_port=cfg.ganesha_nlm_port,
    )
    
    # Write config file
    with open(config_path, "w") as f:
        f.write(config_content)
    
    return str(config_path)


def reload(svm_name: str) -> None:
    """
    Reload NFS-Ganesha service for an SVM.
    
    Args:
        svm_name: SVM name
        
    Raises:
        RuntimeError: If reload fails
    """
    import subprocess

    # Reload using systemctl
    result = subprocess.run(
        ["systemctl", "reload", f"nfs-ganesha@{svm_name}"],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to reload NFS-Ganesha: {result.stderr}")


def add_export(
    svm_name: str,
    volume_name: str,
    client: str,
    access: str = "rw",
    root_squash: bool = True
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
    cfg = load_config()
    export_dir = cfg.export_dir.rstrip("/")
    export_entry = {
        "export_id": export_id,
        "path": f"{export_dir}/{svm_name}/{volume_name}",
        "pseudo": f"{export_dir}/{svm_name}/{volume_name}",
        "access": access.upper(),
        "squash": "Root_Squash" if root_squash else "No_Root_Squash",
        "sec": ["sys"],
        "client": client
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
    cfg = load_config()
    export_dir = cfg.export_dir.rstrip("/")
    exports = [
        e for e in exports
        if not (e.get("path") == f"/exports/{svm_name}/{volume_name}" and e.get("client") == client)
    ]
    # Support old default path too (backward compatibility)
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


def _volume_from_path(path: str) -> str:
    # Expected: <export_dir>/<svm>/<volume> (base dir is configurable)
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""
    return parts[-1]
