"""
SVM management commands.
"""

from typing import Optional

import typer

from arca_storage.cli.lib.ganesha import reload as reload_ganesha
from arca_storage.cli.lib.ganesha import render_config
from arca_storage.cli.lib.netns import attach_vlan, create_namespace, delete_namespace
from arca_storage.cli.lib.pacemaker import create_group, delete_group
from arca_storage.cli.lib.systemd import start_unit, stop_unit
from arca_storage.cli.lib.validators import (validate_ip_cidr, validate_name,
                                   validate_vlan)

app = typer.Typer(help="SVM management commands")


@app.command()
def create(
    name: str = typer.Argument(..., help="SVM name"),
    vlan_id: int = typer.Option(..., "--vlan", help="VLAN ID (1-4094)"),
    ip: str = typer.Option(..., "--ip", help="IP address with CIDR (e.g., 192.168.10.5/24)"),
    gateway: Optional[str] = typer.Option(None, "--gateway", help="Default gateway IP"),
    mtu: int = typer.Option(1500, "--mtu", help="MTU size (default: 1500)"),
):
    """
    Create a new SVM.

    Creates a network namespace, VLAN interface, and configures NFS-Ganesha.
    """
    try:
        # Validate inputs
        validate_name(name)
        validate_vlan(vlan_id)
        validate_ip_cidr(ip)

        # Parse IP and prefix
        ip_addr, prefix = ip.split("/")

        typer.echo(f"Creating SVM: {name}")

        # Create namespace
        create_namespace(name)
        typer.echo(f"  Created namespace: {name}")

        # Attach VLAN
        attach_vlan(name, "bond0", vlan_id, ip, gateway, mtu)
        typer.echo(f"  Configured VLAN {vlan_id} with IP {ip}")

        # Generate ganesha config
        config_path = render_config(name, [])
        typer.echo(f"  Generated ganesha config: {config_path}")

        # Create Pacemaker resource group
        create_group(name, f"/exports/{name}", name, f"nfs-ganesha@{name}")
        typer.echo(f"  Created Pacemaker resource group: g_svm_{name}")

        typer.echo(f"SVM {name} created successfully")

    except Exception as e:
        typer.echo(f"Error creating SVM: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def delete(
    name: str = typer.Argument(..., help="SVM name"),
    force: bool = typer.Option(False, "--force", help="Force deletion even if volumes exist"),
):
    """
    Delete an SVM.

    Removes the network namespace, VLAN interface, and Pacemaker resources.
    """
    try:
        validate_name(name)

        typer.echo(f"Deleting SVM: {name}")

        # Stop Pacemaker resources
        delete_group(name)
        typer.echo(f"  Stopped Pacemaker resource group: g_svm_{name}")

        # Stop ganesha service
        stop_unit(f"nfs-ganesha@{name}")
        typer.echo(f"  Stopped NFS-Ganesha service")

        # Delete namespace (this also removes VLAN interface)
        delete_namespace(name)
        typer.echo(f"  Deleted namespace: {name}")

        typer.echo(f"SVM {name} deleted successfully")

    except Exception as e:
        typer.echo(f"Error deleting SVM: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def list():
    """
    List all SVMs.

    Shows all configured SVMs with their status.
    """
    try:
        # TODO: Implement listing from state file or Pacemaker
        typer.echo("Listing SVMs...")
        typer.echo("(Implementation pending)")

    except Exception as e:
        typer.echo(f"Error listing SVMs: {e}", err=True)
        raise typer.Exit(1)
