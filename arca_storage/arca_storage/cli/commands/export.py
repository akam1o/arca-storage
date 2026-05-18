"""
Export management commands.

Core add/remove/list operations delegate to API services.
Snapshot and rollback utilities still call legacy ganesha helpers.
"""

import json
from typing import List, Optional

import typer

from arca_storage.api.models import ExportCreate
from arca_storage.api.services import export_service
from arca_storage.cli.commands._pagination import list_all_exports, list_all_svms
from arca_storage.cli.lib.ganesha import list_config_snapshots, read_config_snapshot_meta, rollback_config
from arca_storage.cli.lib.state import get_state_dir
from arca_storage.cli.lib.validators import normalize_nfs_client_cidr, validate_ip_cidr, validate_name
from arca_storage.context import get_context

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
        client = normalize_nfs_client_cidr(client)

        if access not in ("rw", "ro"):
            raise ValueError("Access must be 'rw' or 'ro'")

        typer.echo(f"Adding export for volume: {volume} in SVM: {svm}")

        export = export_service.add_export(
            ExportCreate(
                svm=svm,
                volume=volume,
                client=client,
                access=access,
                root_squash=root_squash,
            )
        )

        typer.echo(f"Export added successfully (phase={export['status']})")

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
        export_service.remove_export(svm, volume, client)

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
        exports = list_all_exports(ctx.db, svm=svm, volume=volume)
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
        ctx = get_context()
        if all_svms:
            targets = sorted(
                {
                    str(record.get("spec", {}).get("svm"))
                    for record in list_all_exports(ctx.db)
                    if record.get("spec", {}).get("svm")
                }
                | {
                    str(record.get("spec", {}).get("name"))
                    for record in list_all_svms(ctx.db)
                    if record.get("spec", {}).get("name")
                }
            )
        else:
            if not svm:
                raise ValueError("Specify --svm or --all")
            validate_name(svm)
            targets = [svm]

        if not targets:
            typer.echo("No SVMs found to sync")
            return

        for name in targets:
            path = ctx.export_reconciler.sync_svm_config(name)
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
        ctx = get_context()
        path = rollback_config(svm, config_version, host_network=_host_network_for_svm(ctx, svm))
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
        typer.echo(
            f"protocols={meta.get('protocols')} bind_addr={meta.get('bind_addr')} "
            f"mountd_port={meta.get('mountd_port')} nlm_port={meta.get('nlm_port')}"
        )
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


def _host_network_for_svm(ctx, svm_name: str) -> bool:
    record = ctx.db.get_svm(svm_name)
    if not record:
        return False
    return record.get("spec", {}).get("vlan_id") is None
