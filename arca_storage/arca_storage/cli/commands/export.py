"""
Export management commands.
"""

from typing import List, Optional

import typer

from arca_storage.cli.lib.ganesha import add_export
from arca_storage.cli.lib.ganesha import reload as reload_ganesha
from arca_storage.cli.lib.ganesha import remove_export, render_config
from arca_storage.cli.lib.ganesha import sync as sync_ganesha
from arca_storage.cli.lib.state import get_state_dir
from arca_storage.cli.lib.validators import validate_ip_cidr, validate_name
from arca_storage.cli.lib.ganesha import list_exports as ganesha_list_exports

app = typer.Typer(help="Export management commands")


@app.command()
def add(
    volume: str = typer.Option(..., "--volume", help="Volume name"),
    svm: str = typer.Option(..., "--svm", help="SVM name"),
    client: str = typer.Option(..., "--client", help="Client CIDR (e.g., 10.0.0.0/24)"),
    access: str = typer.Option("rw", "--access", help="Access type: rw or ro (default: rw)"),
    root_squash: bool = typer.Option(True, "--root-squash/--no-root-squash", help="Enable root squash (default: True)"),
):
    """
    Add an NFS export.

    Adds an export entry to the NFS-Ganesha configuration and reloads the service.
    """
    try:
        validate_name(volume)
        validate_name(svm)
        validate_ip_cidr(client)

        if access not in ["rw", "ro"]:
            raise ValueError("Access must be 'rw' or 'ro'")

        typer.echo(f"Adding export for volume: {volume} in SVM: {svm}")

        # Add export to configuration
        add_export(svm, volume, client, access, root_squash)
        typer.echo(f"  Added export: {client} -> {volume}")

        # Reload ganesha
        reload_ganesha(svm)
        typer.echo(f"  Reloaded NFS-Ganesha service")

        typer.echo(f"Export added successfully")

    except Exception as e:
        typer.echo(f"Error adding export: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def remove(
    volume: str = typer.Option(..., "--volume", help="Volume name"),
    svm: str = typer.Option(..., "--svm", help="SVM name"),
    client: str = typer.Option(..., "--client", help="Client CIDR"),
):
    """
    Remove an NFS export.

    Removes an export entry from the NFS-Ganesha configuration and reloads the service.
    """
    try:
        validate_name(volume)
        validate_name(svm)
        validate_ip_cidr(client)

        typer.echo(f"Removing export for volume: {volume} in SVM: {svm}")

        # Remove export from configuration
        remove_export(svm, volume, client)
        typer.echo(f"  Removed export: {client} -> {volume}")

        # Reload ganesha
        reload_ganesha(svm)
        typer.echo(f"  Reloaded NFS-Ganesha service")

        typer.echo(f"Export removed successfully")

    except Exception as e:
        typer.echo(f"Error removing export: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def list(
    svm: Optional[str] = typer.Option(None, "--svm", help="Filter by SVM name"),
    volume: Optional[str] = typer.Option(None, "--volume", help="Filter by volume name"),
):
    """
    List NFS exports.

    Shows all configured exports, optionally filtered by SVM or volume.
    """
    try:
        exports = ganesha_list_exports(svm_name=svm, volume_name=volume)
        if not exports:
            typer.echo("No exports found")
            return
        for exp in exports:
            typer.echo(
                f"{exp.get('svm')}/{exp.get('volume')} client={exp.get('client')} access={exp.get('access')} export_id={exp.get('export_id')}"
            )

    except Exception as e:
        typer.echo(f"Error listing exports: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def sync(
    svm: Optional[str] = typer.Option(None, "--svm", help="SVM name"),
    all_svms: bool = typer.Option(False, "--all", help="Sync all SVMs found in state"),
):
    """
    Re-render ganesha.conf from current state and reload service.

    Useful after changing runtime configuration (e.g., enabling NFSv3).
    """
    try:
        targets: List[str] = []
        if all_svms:
            state_dir = get_state_dir()
            if state_dir.exists():
                for path in sorted(state_dir.glob("exports.*.json")):
                    name = path.name[len("exports.") : -len(".json")]
                    if name:
                        targets.append(name)
        else:
            if not svm:
                raise ValueError("Specify --svm or --all")
            validate_name(svm)
            targets = [svm]

        if not targets:
            typer.echo("No SVMs found to sync")
            return

        for name in targets:
            path = sync_ganesha(name)
            typer.echo(f"Synced: {name} -> {path}")

    except Exception as e:
        typer.echo(f"Error syncing exports: {e}", err=True)
        raise typer.Exit(1)
