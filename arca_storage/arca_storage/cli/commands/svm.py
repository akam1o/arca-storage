"""
SVM management commands.
"""

from typing import Optional

import typer

from arca_storage.cli.lib.ganesha import reload as reload_ganesha
from arca_storage.cli.lib.ganesha import render_config
from arca_storage.cli.lib.netns import attach_vlan, create_namespace, delete_namespace
from arca_storage.cli.lib.pacemaker import create_group, delete_group
from arca_storage.cli.lib.state import delete_svm as state_delete_svm
from arca_storage.cli.lib.state import list_svms as state_list_svms
from arca_storage.cli.lib.state import upsert_svm as state_upsert_svm
from arca_storage.cli.lib.systemd import start_unit, stop_unit
from arca_storage.cli.lib.validators import (
    validate_ip_cidr,
    validate_ipv4,
    infer_gateway_from_ip_cidr,
    validate_name,
    validate_vlan,
)
from arca_storage.cli.lib.lvm import create_lv
from arca_storage.cli.lib.xfs import format_xfs
from arca_storage.cli.lib.config import load_config

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
    """
    Create a new SVM.

    Creates a network namespace, VLAN interface, and configures NFS-Ganesha.
    """
    try:
        # Validate inputs
        validate_name(name)
        validate_vlan(vlan_id)
        validate_ip_cidr(ip)
        if gateway is not None:
            validate_ipv4(gateway)

        # Parse IP and prefix
        ip_addr, prefix = ip.split("/")

        typer.echo(f"Creating SVM: {name}")

        cfg = load_config()
        gateway_ip = gateway or infer_gateway_from_ip_cidr(ip)

        # Create namespace
        create_namespace(name)
        typer.echo(f"  Created namespace: {name}")

        # Attach VLAN
        attach_vlan(name, cfg.parent_if, vlan_id, ip, gateway_ip, mtu)
        typer.echo(f"  Configured VLAN {vlan_id} with IP {ip}")
        typer.echo(f"  Gateway: {gateway_ip}")

        # Generate ganesha config
        config_path = render_config(name, [])
        typer.echo(f"  Generated ganesha config: {config_path}")

        # Optionally create root LV (used by Pacemaker Filesystem resource)
        if root_size:
            vg_name = cfg.vg_name
            lv_name = f"vol_{name}"
            try:
                lv_path = create_lv(vg_name, lv_name, root_size, thin=True, thinpool_name=cfg.thinpool_name)
                format_xfs(lv_path)
                typer.echo(f"  Created root LV: {lv_path}")
            except Exception as e:
                # Keep idempotent-ish behavior if it already exists.
                if "already exists" not in str(e).lower():
                    raise

        export_dir = cfg.export_dir.rstrip("/")

        # Create Pacemaker resource group
        create_group(
            name,
            f"{export_dir}/{name}",
            vlan_id=vlan_id,
            ip=ip_addr,
            prefix=int(prefix),
            gw=gateway_ip,
            mtu=mtu,
            parent_if=cfg.parent_if,
            vg_name=cfg.vg_name,
            drbd_resource_name=(drbd_resource or cfg.drbd_resource),
            create_filesystem=bool(root_size),
        )
        typer.echo(f"  Created Pacemaker resource group: g_svm_{name}")

        state_upsert_svm(
            {
                "name": name,
                "vlan_id": vlan_id,
                "ip_cidr": ip,
                "gateway": gateway_ip,
                "mtu": mtu,
                "namespace": name,
                "vip": ip_addr,
                "status": "available",
            }
        )

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

        state_delete_svm(name)

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
        svms = state_list_svms()
        if not svms:
            typer.echo("No SVMs found")
            return
        for svm in svms:
            typer.echo(
                f"{svm.get('name')} vlan={svm.get('vlan_id')} ip={svm.get('ip_cidr')} status={svm.get('status')}"
            )

    except Exception as e:
        typer.echo(f"Error listing SVMs: {e}", err=True)
        raise typer.Exit(1)
