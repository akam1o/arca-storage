"""
SVM management commands.

Delegates to the SVM reconciler for create/delete operations.
"""

from typing import Optional

import typer

from arca_storage.api.services import svm_service
from arca_storage.cli.lib.validators import (
    validate_ip_cidr,
    validate_ipv4,
    infer_gateway_from_ip_cidr,
    validate_name,
    validate_vlan,
)
from arca_storage.context import get_context
from arca_storage.models.base import Phase
from arca_storage.models.svm import SVM, SVMSpec

app = typer.Typer(help="SVM management commands")


@app.command()
def create(
    name: str = typer.Argument(..., help="SVM name"),
    vlan_id: int = typer.Option(..., "--vlan", help="VLAN ID (1-4094)"),
    ip: str = typer.Option(..., "--ip", help="IP address with CIDR (e.g., 192.168.10.5/24)"),
    gateway: Optional[str] = typer.Option(None, "--gateway", help="Gateway IP (optional; inferred if omitted)"),
    mtu: int = typer.Option(1500, "--mtu", help="MTU size (default: 1500)"),
    root_size: Optional[int] = typer.Option(None, "--root-size", help="Create root LV size in GiB (optional)"),
    drbd_resource: Optional[str] = typer.Option(
        None, "--drbd-resource", help="DRBD resource name for Pacemaker (default: from config or r0)"
    ),
):
    """Create a new SVM via the reconciler."""
    try:
        validate_name(name)
        validate_vlan(vlan_id)
        validate_ip_cidr(ip)
        if gateway is not None:
            validate_ipv4(gateway)

        gateway_ip = gateway or infer_gateway_from_ip_cidr(ip)

        typer.echo(f"Creating SVM: {name}")

        ctx = get_context()
        svm = SVM(
            spec=SVMSpec(
                name=name,
                vlan_id=vlan_id,
                ip_cidr=ip,
                gateway=gateway_ip,
                mtu=mtu,
                root_volume_size_gib=root_size,
            ),
        )

        svm = ctx.svm_reconciler.reconcile(svm)

        if svm.status.phase == Phase.FAILED:
            typer.echo(f"Error creating SVM: {svm.status.message}", err=True)
            raise typer.Exit(1)

        typer.echo(f"SVM {name} created successfully (phase={svm.status.phase.value})")

    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Error creating SVM: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def delete(
    name: str = typer.Argument(..., help="SVM name"),
    force: bool = typer.Option(False, "--force", help="Force cascading deletion of dependent resources"),
    delete_volumes: bool = typer.Option(False, "--delete-volumes", help="Delete dependent volumes before deleting"),
):
    """Delete an SVM after dependent resources are gone or safely cascaded."""
    try:
        validate_name(name)

        typer.echo(f"Deleting SVM: {name}")
        svm_service.delete_svm(name, force=force, delete_volumes=delete_volumes)

        typer.echo(f"SVM {name} deleted successfully")

    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Error deleting SVM: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def list():
    """List all SVMs."""
    try:
        ctx = get_context()
        svms = ctx.db.list_svms()
        if not svms:
            typer.echo("No SVMs found")
            return
        for svm in svms:
            spec = svm.get("spec", {})
            status = svm.get("status", {})
            typer.echo(
                f"{spec.get('name')} vlan={spec.get('vlan_id')} "
                f"ip={spec.get('ip_cidr')} phase={status.get('phase', 'unknown')}"
            )
    except Exception as e:
        typer.echo(f"Error listing SVMs: {e}", err=True)
        raise typer.Exit(1)
