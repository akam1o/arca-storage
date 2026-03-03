"""
Export management commands.

Core add/remove/list operations delegate to the Export reconciler.
Utility commands (sync, snapshots, rollback) still call legacy ganesha helpers.
"""

import json
from typing import List, Optional

import typer

from arca_storage.cli.lib.ganesha import list_config_snapshots, read_config_snapshot_meta, rollback_config
from arca_storage.cli.lib.ganesha import sync as sync_ganesha
from arca_storage.cli.lib.state import get_state_dir
from arca_storage.cli.lib.validators import validate_ip_cidr, validate_name
from arca_storage.context import get_context
from arca_storage.models.base import Phase, ResourceMeta
from arca_storage.models.export import Export, ExportSpec, ExportStatus

app = typer.Typer(help="Export management commands")


@app.command()
def add(
    volume: str = typer.Option(..., "--volume", help="Volume name"),
    svm: str = typer.Option(..., "--svm", help="SVM name"),
    client: str = typer.Option(..., "--client", help="Client CIDR (e.g., 10.0.0.0/24)"),
    access: str = typer.Option("rw", "--access", help="Access type: rw or ro (default: rw)"),
    root_squash: bool = typer.Option(True, "--root-squash/--no-root-squash", help="Enable root squash (default: True)"),
):
    """Add an NFS export via the reconciler."""
    try:
        validate_name(volume)
        validate_name(svm)
        validate_ip_cidr(client)

        if access not in ("rw", "ro"):
            raise ValueError("Access must be 'rw' or 'ro'")

        typer.echo(f"Adding export for volume: {volume} in SVM: {svm}")

        ctx = get_context()
        export = Export(
            spec=ExportSpec(
                svm=svm,
                volume=volume,
                client=client,
                access=access,
                root_squash=root_squash,
            ),
        )
        export = ctx.export_reconciler.reconcile(export)

        if export.status.phase == Phase.FAILED:
            typer.echo(f"Error adding export: {export.status.message}", err=True)
            raise typer.Exit(1)

        typer.echo(f"Export added successfully (phase={export.status.phase.value})")

    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Error adding export: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def remove(
    volume: str = typer.Option(..., "--volume", help="Volume name"),
    svm: str = typer.Option(..., "--svm", help="SVM name"),
    client: str = typer.Option(..., "--client", help="Client CIDR"),
):
    """Remove an NFS export via the reconciler."""
    try:
        validate_name(volume)
        validate_name(svm)
        validate_ip_cidr(client)

        typer.echo(f"Removing export for volume: {volume} in SVM: {svm}")

        ctx = get_context()
        records = ctx.db.list_exports(svm=svm, volume=volume, client=client)
        if not records:
            typer.echo("Export not found", err=True)
            raise typer.Exit(1)

        record = records[0]
        export = Export(
            metadata=ResourceMeta(id=record["id"], generation=record.get("generation", 1)),
            spec=ExportSpec.model_validate(record["spec"]),
            status=ExportStatus.model_validate(record["status"]),
        )
        export.status.phase = Phase.DELETING
        ctx.export_reconciler.reconcile(export)

        typer.echo("Export removed successfully")

    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Error removing export: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def list(
    svm: Optional[str] = typer.Option(None, "--svm", help="Filter by SVM name"),
    volume: Optional[str] = typer.Option(None, "--volume", help="Filter by volume name"),
):
    """List NFS exports from the database."""
    try:
        ctx = get_context()
        exports = ctx.db.list_exports(svm=svm, volume=volume)
        if not exports:
            typer.echo("No exports found")
            return
        for exp in exports:
            spec = exp.get("spec", {})
            status = exp.get("status", {})
            typer.echo(
                f"{spec.get('svm')}/{spec.get('volume')} "
                f"client={spec.get('client')} "
                f"access={spec.get('access')} "
                f"phase={status.get('phase', 'unknown')}"
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


@app.command()
def snapshots(
    svm: str = typer.Option(..., "--svm", help="SVM name"),
):
    """
    List saved ganesha.conf snapshots for an SVM.
    """
    try:
        validate_name(svm)
        snaps = list_config_snapshots(svm)
        if not snaps:
            typer.echo("No snapshots found")
            return
        for s in snaps:
            typer.echo(f"{s.get('config_version')} {s.get('path')}")
        typer.echo(f"latest {get_state_dir()}/config/ganesha.{svm}.latest.conf")
    except Exception as e:
        typer.echo(f"Error listing snapshots: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def rollback(
    svm: str = typer.Option(..., "--svm", help="SVM name"),
    config_version: str = typer.Option("latest", "--config-version", help="Snapshot version (default: latest)"),
):
    """
    Roll back ganesha.<svm>.conf to a saved snapshot and reload.
    """
    try:
        validate_name(svm)
        path = rollback_config(svm, config_version)
        typer.echo(f"Rolled back: {svm} -> {path} (version={config_version})")
    except Exception as e:
        typer.echo(f"Error rolling back: {e}", err=True)
        raise typer.Exit(1)


@app.command("snapshot-show")
def snapshot_show(
    svm: str = typer.Option(..., "--svm", help="SVM name"),
    config_version: str = typer.Option("latest", "--config-version", help="Snapshot version (default: latest)"),
    as_json: bool = typer.Option(False, "--json", help="Print raw snapshot metadata as JSON"),
):
    """
    Show what a snapshot contains (protocols/ports/exports).
    """
    try:
        validate_name(svm)
        meta = read_config_snapshot_meta(svm, config_version)
        if as_json:
            typer.echo(typer.style(json.dumps(meta, indent=2, ensure_ascii=False, sort_keys=True), dim=False))
            return

        typer.echo(f"svm={svm} config_version={meta.get('config_version')} template_version={meta.get('template_version')}")
        typer.echo(f"protocols={meta.get('protocols')} mountd_port={meta.get('mountd_port')} nlm_port={meta.get('nlm_port')}")
        exports = meta.get("exports") or []
        if not exports:
            typer.echo("exports: (none)")
            return
        typer.echo("exports:")
        for e in exports:
            typer.echo(
                f"  id={e.get('export_id')} client={e.get('client')} access={e.get('access')} "
                f"sec={e.get('sec')} squash={e.get('squash')} path={e.get('path')}"
            )
    except Exception as e:
        typer.echo(f"Error showing snapshot: {e}", err=True)
        raise typer.Exit(1)
