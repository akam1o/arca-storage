"""
Export service layer.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from arca_storage.api.models import ExportCreate, ExportStatus
from arca_storage.cli.lib.ganesha import add_export as ganesha_add_export
from arca_storage.cli.lib.ganesha import list_exports as ganesha_list_exports
from arca_storage.cli.lib.ganesha import remove_export as ganesha_remove_export
from arca_storage.cli.lib.validators import validate_ip_cidr, validate_name
from arca_storage.cli.lib.config import load_config
from arca_storage.cli.lib.state import get_state_dir


async def add_export(export_data: ExportCreate) -> Dict[str, Any]:
    """
    Add an NFS export.

    Args:
        export_data: Export creation data

    Returns:
        Export dictionary
    """
    validate_name(export_data.svm)
    validate_name(export_data.volume)
    validate_ip_cidr(export_data.client)

    # Add export to configuration
    ganesha_add_export(
        export_data.svm, export_data.volume, export_data.client, export_data.access, export_data.root_squash
    )

    # Load exports to get export_id
    state_dir = get_state_dir()
    state_file = state_dir / f"exports.{export_data.svm}.json"
    if state_file.exists():
        with open(state_file, "r") as f:
            exports = json.load(f)
    else:
        exports = []
    export_entry = next(
        (
            e
            for e in exports
            if e.get("client") == export_data.client
            and e.get("path") == f"{load_config().export_dir.rstrip('/')}/{export_data.svm}/{export_data.volume}"
        ),
        None,
    )

    if not export_entry:
        raise RuntimeError("Failed to create export")

    return {
        "svm": export_data.svm,
        "volume": export_data.volume,
        "client": export_data.client,
        "access": export_data.access,
        "root_squash": export_data.root_squash,
        "sec": export_data.sec,
        "pseudo": export_entry.get("pseudo", f"/exports/{export_data.svm}/{export_data.volume}"),
        "export_id": export_entry.get("export_id", 0),
        "status": ExportStatus.AVAILABLE.value,
        "created_at": datetime.utcnow(),
    }


async def remove_export(svm: str, volume: str, client: str) -> None:
    """
    Remove an NFS export.

    Args:
        svm: SVM name
        volume: Volume name
        client: Client CIDR
    """
    validate_name(svm)
    validate_name(volume)
    validate_ip_cidr(client)

    ganesha_remove_export(svm, volume, client)


async def list_exports(
    svm: Optional[str] = None,
    volume: Optional[str] = None,
    client: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """
    List exports.

    Args:
        svm: Filter by SVM name
        volume: Filter by volume name
        client: Filter by client CIDR
        limit: Maximum results
        cursor: Pagination cursor

    Returns:
        Dictionary with items and next_cursor
    """
    items = ganesha_list_exports(svm_name=svm, volume_name=volume)
    if client:
        items = [i for i in items if i.get("client") == client]
    return {"items": items[:limit], "next_cursor": None}
