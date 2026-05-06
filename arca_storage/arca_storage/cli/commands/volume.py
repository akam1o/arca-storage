"""
Volume management commands.

Delegates mutating operations to API services.
"""

from typing import Optional

import typer

from arca_storage.api.models import VolumeCreate
from arca_storage.api.services import volume_service
from arca_storage.cli.lib.validators import validate_name
from arca_storage.context import get_context

app = typer.Typer(help="Volume management commands")


@app.command()
def create(
    name: str = typer.Argument(..., help="Volume name"),
    svm: str = typer.Option(..., "--svm", help="SVM name"),
    size: int = typer.Option(..., "--size", help="Size in GiB"),
    thin: bool = typer.Option(True, "--thin/--no-thin", help="Use thin provisioning (default: True)"),
):
    """Create a new volume."""
    try:
        validate_name(name)
        validate_name(svm)

        typer.echo(f"Creating volume: {name} in SVM: {svm}")

        volume = volume_service.create_volume(
            VolumeCreate(name=name, svm=svm, size_gib=size, thin=thin, fs_type="xfs")
        )

        typer.echo(f"Volume {name} created successfully (phase={volume['status']})")

    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Error creating volume: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def resize(
    name: str = typer.Argument(..., help="Volume name"),
    svm: str = typer.Option(..., "--svm", help="SVM name"),
    new_size: int = typer.Option(..., "--new-size", help="New size in GiB"),
):
    """Resize a volume (LVM extend + XFS grow)."""
    try:
        validate_name(name)
        validate_name(svm)

        typer.echo(f"Resizing volume: {name} in SVM: {svm}")

        volume_service.resize_volume(name, svm, new_size)

        typer.echo(f"Volume {name} resized successfully")

    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Error resizing volume: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def delete(
    name: str = typer.Argument(..., help="Volume name"),
    svm: str = typer.Option(..., "--svm", help="SVM name"),
    force: bool = typer.Option(False, "--force", help="Delete dependent snapshots before deleting"),
):
    """Delete a volume after dependent exports/snapshots are handled."""
    try:
        validate_name(name)
        validate_name(svm)

        typer.echo(f"Deleting volume: {name} in SVM: {svm}")
        volume_service.delete_volume(name, svm, force=force)

        typer.echo(f"Volume {name} deleted successfully")

    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Error deleting volume: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def list(
    svm: Optional[str] = typer.Option(None, "--svm", help="Filter by SVM name"),
    name: Optional[str] = typer.Option(None, "--name", help="Filter by volume name"),
):
    """List volumes."""
    try:
        ctx = get_context()
        volumes = ctx.db.list_volumes(svm=svm, name=name)
        if not volumes:
            typer.echo("No volumes found")
            return
        for vol in volumes:
            spec = vol.get("spec", {})
            status = vol.get("status", {})
            typer.echo(
                f"{spec.get('svm')}/{spec.get('name')} "
                f"size={spec.get('size_gib')}GiB "
                f"phase={status.get('phase', 'unknown')} "
                f"mount={status.get('mount_path', 'N/A')}"
            )
    except Exception as e:
        typer.echo(f"Error listing volumes: {e}", err=True)
        raise typer.Exit(1)
