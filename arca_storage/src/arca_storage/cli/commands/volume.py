"""
Volume management commands.
"""

from typing import Optional

import typer

from arca_storage.cli.lib.lvm import create_lv, delete_lv, resize_lv
from arca_storage.cli.lib.validators import validate_name
from arca_storage.cli.lib.xfs import format_xfs, grow_xfs, mount_xfs, umount_xfs

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

        # Determine VG name (assuming vg_pool_01 for now)
        vg_name = "vg_pool_01"
        mount_path = f"/exports/{svm}/{name}"

        # Create LV
        lv_path = create_lv(vg_name, name, size, thin=thin)
        typer.echo(f"  Created LV: {lv_path}")

        # Format XFS
        format_xfs(lv_path)
        typer.echo(f"  Formatted XFS filesystem")

        # Mount
        mount_xfs(lv_path, mount_path)
        typer.echo(f"  Mounted at: {mount_path}")

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

        vg_name = "vg_pool_01"
        mount_path = f"/exports/{svm}/{name}"

        # Resize LV
        resize_lv(vg_name, name, new_size)
        typer.echo(f"  Resized LV to {new_size} GiB")

        # Grow XFS
        grow_xfs(mount_path)
        typer.echo(f"  Grew XFS filesystem")

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

        vg_name = "vg_pool_01"
        mount_path = f"/exports/{svm}/{name}"

        # Unmount
        umount_xfs(mount_path)
        typer.echo(f"  Unmounted filesystem")

        # Delete LV
        delete_lv(vg_name, name)
        typer.echo(f"  Deleted LV")

        typer.echo(f"Volume {name} deleted successfully")

    except Exception as e:
        typer.echo(f"Error deleting volume: {e}", err=True)
        raise typer.Exit(1)
