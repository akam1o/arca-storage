"""
Bootstrap commands for initial setup without Ansible.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import typer

from arca_storage.cli.lib.config import load_config

app = typer.Typer(help="Bootstrap initial system/cluster configuration")


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _run_shell(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-lc", command], capture_output=True, text=True, check=True)


def _resource_path(*parts: str) -> Path:
    # arca_storage/cli/commands/bootstrap.py -> arca_storage/resources/...
    return Path(__file__).resolve().parents[2] / "resources" / Path(*parts)


def _render_env(cfg) -> str:
    lines = [
        "# Managed by arca bootstrap (derived values for systemd units)",
        f"ARCA_GANESHA_CONFIG_DIR={cfg.ganesha_config_dir}",
        f"ARCA_EXPORT_DIR={cfg.export_dir}",
        f"ARCA_API_HOST={cfg.api_host}",
        f"ARCA_API_PORT={cfg.api_port}",
    ]
    if cfg.state_dir:
        lines.append(f"ARCA_STATE_DIR={cfg.state_dir}")
    return "\n".join(lines) + "\n"


def _write_env_file(cfg) -> Path:
    env_dst_dir = Path("/etc/arca-storage")
    env_dst_dir.mkdir(parents=True, exist_ok=True)
    env_dst = env_dst_dir / "arca-storage.env"
    env_dst.write_text(_render_env(cfg), encoding="utf-8")
    return env_dst


@app.command()
def install(
    ra_vendor: str = typer.Option("local", help="OCF vendor directory name (default: local)"),
    install_api_service: bool = typer.Option(True, help="Install arca-storage-api systemd unit"),
    install_ganesha_unit: bool = typer.Option(True, help="Install nfs-ganesha@.service systemd unit"),
    install_config: bool = typer.Option(
        True, help="Install /etc/arca-storage/storage-bootstrap.conf and storage-runtime.conf if missing"
    ),
):
    """
    Install local resource files (Pacemaker RA, systemd unit files).

    This command is designed to be idempotent.
    """
    try:
        cfg = load_config()
        if install_config:
            cfg_dst_dir = Path("/etc/arca-storage")
            cfg_dst_dir.mkdir(parents=True, exist_ok=True)

            bootstrap_src = _resource_path("config", "storage-bootstrap.conf")
            bootstrap_dst = cfg_dst_dir / "storage-bootstrap.conf"
            if bootstrap_src.exists() and not bootstrap_dst.exists():
                shutil.copy2(bootstrap_src, bootstrap_dst)

            runtime_src = _resource_path("config", "storage-runtime.conf")
            runtime_dst = cfg_dst_dir / "storage-runtime.conf"
            if runtime_src.exists() and not runtime_dst.exists():
                shutil.copy2(runtime_src, runtime_dst)

            # Reload config after installing files so derived env matches.
            cfg = load_config()

        # Pacemaker RA
        ra_src = _resource_path("pacemaker", "NetnsVlan")
        if not ra_src.exists():
            raise RuntimeError(f"Missing packaged RA: {ra_src}")

        ra_dst_dir = Path(f"/usr/lib/ocf/resource.d/{ra_vendor or cfg.pacemaker_ra_vendor}")
        ra_dst_dir.mkdir(parents=True, exist_ok=True)
        ra_dst = ra_dst_dir / "NetnsVlan"
        shutil.copy2(ra_src, ra_dst)
        os.chmod(ra_dst, 0o755)

        # systemd units
        if install_api_service:
            api_src = _resource_path("systemd", "arca-storage-api.service")
            api_dst = Path("/etc/systemd/system/arca-storage-api.service")
            if api_src.exists():
                shutil.copy2(api_src, api_dst)

        if install_ganesha_unit:
            ganesha_src = _resource_path("systemd", "nfs-ganesha@.service")
            ganesha_dst = Path("/etc/systemd/system/nfs-ganesha@.service")
            if not ganesha_src.exists():
                raise RuntimeError(f"Missing packaged systemd unit: {ganesha_src}")
            shutil.copy2(ganesha_src, ganesha_dst)

        # systemd environment file (used by nfs-ganesha@.service)
        _write_env_file(cfg)

        _run(["systemctl", "daemon-reload"])
        typer.echo("Installed bootstrap resources successfully")
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command("render-env")
def render_env():
    """
    Re-generate /etc/arca-storage/arca-storage.env from current config files.

    Use this after editing storage-bootstrap.conf / storage-runtime.conf.
    """
    try:
        cfg = load_config()
        env_path = _write_env_file(cfg)
        typer.echo(f"Wrote {env_path}")
        _run(["systemctl", "daemon-reload"], check=False)
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def verify(
    strict: bool = typer.Option(False, help="Exit non-zero if any checks fail"),
    check_system: bool = typer.Option(True, help="Run system/cluster status checks (pcs/drbd/lvm/systemd)"),
):
    """
    Verify prerequisites and installed files for bootstrap/runtime.

    This command is non-destructive.
    """
    cfg = load_config()
    issues: list[str] = []

    def check(cond: bool, ok: str, bad: str) -> None:
        if cond:
            typer.echo(f"OK: {ok}")
        else:
            typer.echo(f"NG: {bad}", err=True)
            issues.append(bad)

    # Config files
    check(Path("/etc/arca-storage/storage-bootstrap.conf").exists(), "bootstrap config present", "missing storage-bootstrap.conf")
    check(Path("/etc/arca-storage/storage-runtime.conf").exists(), "runtime config present", "missing storage-runtime.conf")

    # systemd env
    check(
        Path("/etc/arca-storage/arca-storage.env").exists(),
        "arca-storage.env present",
        "missing arca-storage.env (run: arca bootstrap render-env)",
    )

    # Key binaries (presence only)
    for binary in ["systemctl", "pcs", "drbdadm", "pvcreate", "vgcreate", "lvcreate", "ganesha.nfsd", "ip"]:
        check(shutil.which(binary) is not None, f"found binary: {binary}", f"missing binary in PATH: {binary}")

    # Pacemaker RA
    ra_path = Path(f"/usr/lib/ocf/resource.d/{cfg.pacemaker_ra_vendor}/NetnsVlan")
    check(
        ra_path.exists(),
        f"NetnsVlan RA installed at {ra_path}",
        f"missing NetnsVlan RA at {ra_path} (run: arca bootstrap install)",
    )

    # systemd unit files
    check(Path("/etc/systemd/system/nfs-ganesha@.service").exists(), "nfs-ganesha@.service present", "missing nfs-ganesha@.service (run: arca bootstrap install)")
    check(Path("/etc/systemd/system/arca-storage-api.service").exists(), "arca-storage-api.service present", "missing arca-storage-api.service (run: arca bootstrap install)")

    # Config sanity (basic)
    check(cfg.export_dir.startswith("/"), f"export_dir={cfg.export_dir}", f"export_dir must be absolute: {cfg.export_dir}")
    check(
        cfg.ganesha_config_dir.startswith("/"),
        f"ganesha_config_dir={cfg.ganesha_config_dir}",
        f"ganesha_config_dir must be absolute: {cfg.ganesha_config_dir}",
    )
    check(bool(cfg.vg_name), f"vg_name={cfg.vg_name}", "vg_name is empty")
    check(bool(cfg.parent_if), f"parent_if={cfg.parent_if}", "parent_if is empty")
    check(bool(cfg.drbd_resource), f"drbd_resource={cfg.drbd_resource}", "drbd_resource is empty")

    if check_system:
        # systemd health (only if systemctl exists)
        if shutil.which("systemctl"):
            for unit in ["pcsd", "corosync", "pacemaker"]:
                res = _run(["systemctl", "is-active", unit], check=False)
                check(res.returncode == 0, f"systemd {unit} is active", f"systemd {unit} is not active")
        else:
            check(False, "systemctl available", "systemctl not found; cannot verify services")

        # Pacemaker cluster health
        if shutil.which("pcs"):
            res = _run(["pcs", "status"], check=False)
            check(res.returncode == 0, "pcs status ok", f"pcs status failed: {(res.stderr or res.stdout).strip()}")

            master = f"ms_drbd_{cfg.drbd_resource}"
            res = _run(["pcs", "resource", "show", master], check=False)
            check(res.returncode == 0, f"Pacemaker DRBD master present: {master}", f"missing Pacemaker DRBD master: {master}")
        else:
            check(False, "pcs available", "pcs not found; cannot verify cluster resources")

        # DRBD status
        if shutil.which("drbdadm"):
            res = _run(["drbdadm", "status", cfg.drbd_resource], check=False)
            check(
                res.returncode == 0,
                f"drbdadm status ok: {cfg.drbd_resource}",
                f"drbdadm status failed for {cfg.drbd_resource}: {(res.stderr or res.stdout).strip()}",
            )
        else:
            check(False, "drbdadm available", "drbdadm not found; cannot verify DRBD")

        # LVM status
        if shutil.which("vgs") and shutil.which("lvs"):
            res = _run(["vgs", cfg.vg_name], check=False)
            check(res.returncode == 0, f"VG present: {cfg.vg_name}", f"missing VG: {cfg.vg_name}")
            res = _run(["lvs", f"{cfg.vg_name}/{cfg.thinpool_name}"], check=False)
            check(
                res.returncode == 0,
                f"Thin pool present: {cfg.vg_name}/{cfg.thinpool_name}",
                f"missing thin pool: {cfg.vg_name}/{cfg.thinpool_name}",
            )
        else:
            check(False, "lvm tools available", "vgs/lvs not found; cannot verify LVM")

        # Directories
        check(Path(cfg.export_dir).exists(), f"export_dir exists: {cfg.export_dir}", f"missing export_dir: {cfg.export_dir}")
        check(
            Path(cfg.ganesha_config_dir).exists(),
            f"ganesha_config_dir exists: {cfg.ganesha_config_dir}",
            f"missing ganesha_config_dir: {cfg.ganesha_config_dir}",
        )

    if strict and issues:
        raise typer.Exit(2)


@app.command()
def pacemaker_cluster(
    cluster_name: str = typer.Option(..., help="Cluster name"),
    nodes: str = typer.Option(..., help="Space-separated node names (must resolve)"),
    hacluster_password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True),
    stonith_enabled: bool = typer.Option(False, help="Set stonith-enabled property"),
):
    """
    Bootstrap Pacemaker/Corosync cluster using pcs.

    This runs locally and configures the cluster across the provided nodes.
    """
    try:
        node_list = [n for n in nodes.split() if n]
        if len(node_list) < 2:
            raise ValueError("Provide at least 2 nodes")

        # Ensure pcsd is running
        _run(["systemctl", "enable", "--now", "pcsd"])

        # Ensure hacluster password
        subprocess.run(
            ["chpasswd"],
            input=f"hacluster:{hacluster_password}\n",
            capture_output=True,
            text=True,
            check=True,
        )

        # Authenticate and setup
        auth = _run(["pcs", "host", "auth", *node_list, "-u", "hacluster", "-p", hacluster_password], check=False)
        if auth.returncode != 0 and "Authorized" not in (auth.stdout or ""):
            raise RuntimeError(f"pcs host auth failed: {auth.stderr.strip()}")

        if not Path("/etc/corosync/authkey").exists():
            setup = _run(["pcs", "cluster", "setup", "--name", cluster_name, *node_list], check=False)
            if setup.returncode != 0 and "already exists" not in (setup.stderr or "").lower():
                raise RuntimeError(f"pcs cluster setup failed: {setup.stderr.strip()}")

        _run(["pcs", "cluster", "start", "--all"])
        _run(["pcs", "cluster", "enable", "--all"])

        stonith_value = "true" if stonith_enabled else "false"
        _run(["pcs", "property", "set", f"stonith-enabled={stonith_value}"])

        typer.echo("Pacemaker cluster bootstrap completed")
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def drbd_config(
    resource: str = typer.Option("r0", help="DRBD resource name (default: r0)"),
    device: str = typer.Option("/dev/drbd0", help="DRBD device path (default: /dev/drbd0)"),
    disk: str = typer.Option(..., help="Backing disk/partition (e.g., /dev/nvme0n1p1)"),
    node1: str = typer.Option(..., help="Node1 hostname (matches uname/pcs)"),
    node1_ip: str = typer.Option(..., help="Node1 replication IP"),
    node2: str = typer.Option(..., help="Node2 hostname (matches uname/pcs)"),
    node2_ip: str = typer.Option(..., help="Node2 replication IP"),
    port: int = typer.Option(7788, help="Replication port (default: 7788)"),
    apply: bool = typer.Option(False, help="Run drbdadm create-md/up after writing config"),
    primary: bool = typer.Option(False, help="Promote this node to primary (requires --apply)"),
):
    """
    Write DRBD resource configuration to /etc/drbd.d/<resource>.res.
    """
    try:
        dest_dir = Path("/etc/drbd.d")
        dest_dir.mkdir(parents=True, exist_ok=True)
        res_path = dest_dir / f"{resource}.res"
        res_content = (
            f"resource {resource} {{\n"
            f"  protocol C;\n"
            f"  meta-disk internal;\n\n"
            f"  on {node1} {{\n"
            f"    device {device};\n"
            f"    disk {disk};\n"
            f"    address {node1_ip}:{port};\n"
            f"  }}\n"
            f"  on {node2} {{\n"
            f"    device {device};\n"
            f"    disk {disk};\n"
            f"    address {node2_ip}:{port};\n"
            f"  }}\n"
            f"}}\n"
        )
        res_path.write_text(res_content, encoding="utf-8")
        typer.echo(f"Wrote DRBD resource config: {res_path}")

        if apply:
            _run(["drbdadm", "create-md", resource], check=False)
            _run(["drbdadm", "up", resource], check=False)
            if primary:
                _run(["drbdadm", "primary", "--force", resource], check=False)
            typer.echo("Applied DRBD configuration")
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def lvm_thinpool(
    pv: str = typer.Option("/dev/drbd0", help="PV device path (default: /dev/drbd0)"),
    vg: Optional[str] = typer.Option(None, help="Volume group name (default: from config or vg_pool_01)"),
    thinpool: Optional[str] = typer.Option(None, help="Thin pool LV name (default: from config or pool)"),
    size: str = typer.Option("80%VG", help="Thin pool size for lvcreate -L (default: 80%VG)"),
    metadata_size: str = typer.Option("15.8G", help="Thin pool metadata size (default: 15.8G)"),
    chunk_size: str = typer.Option("256K", help="Thin pool chunk size (default: 256K)"),
):
    """
    Create PV/VG/thinpool required by arca on the local node.
    """
    try:
        cfg = load_config()
        vg = vg or cfg.vg_name
        thinpool = thinpool or cfg.thinpool_name

        # PV
        pv_check = _run(["pvs", pv], check=False)
        if pv_check.returncode != 0:
            _run(["pvcreate", pv])

        # VG
        vg_check = _run(["vgs", vg], check=False)
        if vg_check.returncode != 0:
            _run(["vgcreate", vg, pv])

        # Thinpool
        lv_path = f"{vg}/{thinpool}"
        lv_check = _run(["lvs", lv_path], check=False)
        if lv_check.returncode != 0:
            _run(
                [
                    "lvcreate",
                    "-L",
                    size,
                    "-T",
                    lv_path,
                    "-c",
                    chunk_size,
                    "--poolmetadatasize",
                    metadata_size,
                    "-Z",
                    "y",
                ]
            )

        _run(["systemctl", "enable", "--now", "lvm2-monitor"], check=False)
        typer.echo("LVM thin pool bootstrap completed")
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
