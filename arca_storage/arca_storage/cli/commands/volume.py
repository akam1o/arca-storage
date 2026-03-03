"""
Volume management commands.

Delegates to the Volume reconciler for create/delete operations.
"""

from typing import Optional

import typer

from arca_storage.cli.lib.validators import validate_name
from arca_storage.context import get_context
from arca_storage.models.base import Phase, ResourceMeta
from arca_storage.models.volume import Volume, VolumeSpec, VolumeStatus

app = typer.Typer(help="Volume management commands")


@app.command()
def create(
    name: str = typer.Argument(..., help="Volume name"),
    svm: str = typer.Option(..., "--svm", help="SVM name"),
    size: int = typer.Option(..., "--size", help="Size in GiB"),
    thin: bool = typer.Option(True, "--thin/--no-thin", help="Use thin provisioning (default: True)"),
):
    """Create a new volume via the reconciler."""
    try:
        validate_name(name)
        validate_name(svm)

        typer.echo(f"Creating volume: {name} in SVM: {svm}")

        ctx = get_context()
        volume = Volume(
            spec=VolumeSpec(name=name, svm=svm, size_gib=size, thin=thin),
        )
        volume = ctx.volume_reconciler.reconcile(volume)

        if volume.status.phase == Phase.FAILED:
            typer.echo(f"Error creating volume: {volume.status.message}", err=True)
            raise typer.Exit(1)

        typer.echo(f"Volume {name} created successfully (phase={volume.status.phase.value})")

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

        ctx = get_context()
        cfg = ctx.settings.to_reconciler_config()
        vg_name = cfg["vg_name"]
        export_dir = cfg["export_dir"]
        lv_name = f"vol_{svm}_{name}"
        mount_path = f"{export_dir}/{svm}/{name}"

        ctx.adapters.lvm.resize_lv(vg_name, lv_name, new_size)
        typer.echo(f"  Resized LV to {new_size} GiB")

        ctx.adapters.xfs.grow(mount_path)
        typer.echo(f"  Grew XFS filesystem")

        # Update DB record
        records = ctx.db.list_volumes(svm=svm, name=name)
        if records:
            record = records[0]
            vol = Volume(
                metadata=ResourceMeta(id=record["id"], generation=record.get("generation", 1)),
                spec=VolumeSpec.model_validate(record["spec"]),
                status=VolumeStatus.model_validate(record["status"]),
            )
            vol.spec.size_gib = new_size
            vol.metadata.bump()
            ctx.db.upsert_volume(vol)

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
    force: bool = typer.Option(False, "--force", help="Force deletion"),
):
    """Delete a volume via the reconciler."""
    try:
        validate_name(name)
        validate_name(svm)

        typer.echo(f"Deleting volume: {name} in SVM: {svm}")

        ctx = get_context()
        records = ctx.db.list_volumes(svm=svm, name=name)
        if not records:
            typer.echo(f"Volume {name} not found in SVM {svm}", err=True)
            raise typer.Exit(1)

        record = records[0]
        vol = Volume(
            metadata=ResourceMeta(id=record["id"], generation=record.get("generation", 1)),
            spec=VolumeSpec.model_validate(record["spec"]),
            status=VolumeStatus.model_validate(record["status"]),
        )
        vol.status.phase = Phase.DELETING
        ctx.volume_reconciler.reconcile(vol)

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
