"""
Volume management commands.
"""

from typing import Optional

import typer

from arca_storage.cli.lib.lvm import create_lv, delete_lv, resize_lv
from arca_storage.cli.lib.state import delete_volume as state_delete_volume
from arca_storage.cli.lib.state import list_volumes as state_list_volumes
from arca_storage.cli.lib.state import upsert_volume as state_upsert_volume
from arca_storage.cli.lib.validators import validate_name
from arca_storage.cli.lib.xfs import format_xfs, grow_xfs, mount_xfs, umount_xfs
from arca_storage.cli.lib.config import load_config

app = typer.Typer(help="Volume management commands")


@app.command()
def create(
    name: str = typer.Argument(..., help="Volume name"),
    svm: str = typer.Option(..., "--svm", help="SVM name"),
    size: int = typer.Option(..., "--size", help="Size in GiB"),
    thin: bool = typer.Option(True, "--thin/--no-thin", help="Use thin provisioning (default: True)"),
):
    """
    Create a new volume.

    Creates an LVM logical volume, formats it with XFS, and mounts it.
    """
    try:
        validate_name(name)
        validate_name(svm)

        typer.echo(f"Creating volume: {name} in SVM: {svm}")

        cfg = load_config()
        vg_name = cfg.vg_name
        export_dir = cfg.export_dir.rstrip("/")
        mount_path = f"{export_dir}/{svm}/{name}"
        lv_name = f"vol_{svm}_{name}"

        # Create LV
        lv_path = create_lv(vg_name, lv_name, size, thin=thin, thinpool_name=cfg.thinpool_name)
        typer.echo(f"  Created LV: {lv_path}")

        # Format XFS
        format_xfs(lv_path)
        typer.echo(f"  Formatted XFS filesystem")

        # Mount
        mount_xfs(lv_path, mount_path)
        typer.echo(f"  Mounted at: {mount_path}")

        state_upsert_volume(
            {
                "name": name,
                "svm": svm,
                "size_gib": size,
                "thin": thin,
                "fs_type": "xfs",
                "mount_path": mount_path,
                "lv_path": lv_path,
                "lv_name": lv_name,
                "status": "available",
            }
        )

        typer.echo(f"Volume {name} created successfully")

    except Exception as e:
        typer.echo(f"Error creating volume: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def resize(
    name: str = typer.Argument(..., help="Volume name"),
    svm: str = typer.Option(..., "--svm", help="SVM name"),
    new_size: int = typer.Option(..., "--new-size", help="New size in GiB"),
):
    """
    Resize a volume.

    Extends the LVM logical volume and grows the XFS filesystem.
    """
    try:
        validate_name(name)
        validate_name(svm)

        typer.echo(f"Resizing volume: {name} in SVM: {svm}")

        cfg = load_config()
        vg_name = cfg.vg_name
        export_dir = cfg.export_dir.rstrip("/")
        mount_path = f"{export_dir}/{svm}/{name}"
        lv_name = f"vol_{svm}_{name}"

        # Resize LV
        resize_lv(vg_name, lv_name, new_size)
        typer.echo(f"  Resized LV to {new_size} GiB")

        # Grow XFS
        grow_xfs(mount_path)
        typer.echo(f"  Grew XFS filesystem")

        state_upsert_volume(
            {
                "name": name,
                "svm": svm,
                "size_gib": new_size,
                "thin": True,
                "fs_type": "xfs",
                "mount_path": mount_path,
                "lv_path": f"/dev/{vg_name}/{lv_name}",
                "lv_name": lv_name,
                "status": "available",
            }
        )

        typer.echo(f"Volume {name} resized successfully")

    except Exception as e:
        typer.echo(f"Error resizing volume: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def delete(
    name: str = typer.Argument(..., help="Volume name"),
    svm: str = typer.Option(..., "--svm", help="SVM name"),
    force: bool = typer.Option(False, "--force", help="Force deletion"),
):
    """
    Delete a volume.

    Unmounts the filesystem and removes the LVM logical volume.
    """
    try:
        validate_name(name)
        validate_name(svm)

        typer.echo(f"Deleting volume: {name} in SVM: {svm}")

        cfg = load_config()
        vg_name = cfg.vg_name
        export_dir = cfg.export_dir.rstrip("/")
        mount_path = f"{export_dir}/{svm}/{name}"
        lv_name = f"vol_{svm}_{name}"

        # Unmount
        umount_xfs(mount_path)
        typer.echo(f"  Unmounted filesystem")

        # Delete LV
        delete_lv(vg_name, lv_name)
        typer.echo(f"  Deleted LV")

        state_delete_volume(svm, name)

        typer.echo(f"Volume {name} deleted successfully")

    except Exception as e:
        typer.echo(f"Error deleting volume: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def list(
    svm: Optional[str] = typer.Option(None, "--svm", help="Filter by SVM name"),
    name: Optional[str] = typer.Option(None, "--name", help="Filter by volume name"),
):
    """
    List volumes.
    """
    try:
        volumes = state_list_volumes(svm=svm, name=name)
        if not volumes:
            typer.echo("No volumes found")
            return
        for vol in volumes:
            typer.echo(
                f"{vol.get('svm')}/{vol.get('name')} size={vol.get('size_gib')}GiB thin={vol.get('thin')} mount={vol.get('mount_path')}"
            )
    except Exception as e:
        typer.echo(f"Error listing volumes: {e}", err=True)
        raise typer.Exit(1)
