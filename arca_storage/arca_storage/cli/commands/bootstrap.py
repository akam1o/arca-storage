"""
Bootstrap commands for initial setup without Ansible.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Optional

import typer

from arca_storage.config import DEFAULT_CONFIG_PATH, load_settings

app = typer.Typer(help="Bootstrap initial system/cluster configuration")

_DEFAULT_COMMAND_TIMEOUT_SECONDS = 30
_DEVICE_PATH_RE = re.compile(r"/dev/[A-Za-z0-9._/+:-]+")
_HOST_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    input: Optional[str] = None,
    timeout: int = _DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            input=input,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{' '.join(cmd)} timed out after {timeout}s") from e


def _run_shell(command: str) -> subprocess.CompletedProcess[str]:
    return _run(["bash", "-lc", command])


def _resource_path(*parts: str) -> Path:
    # arca_storage/cli/commands/bootstrap.py -> arca_storage/resources/...
    return Path(__file__).resolve().parents[2] / "resources" / Path(*parts)


def _render_env(cfg) -> str:
    return cfg.to_systemd_env()


def _pcs_host_auth(nodes: list[str], hacluster_password: str) -> subprocess.CompletedProcess[str]:
    return _run(
        ["pcs", "host", "auth", *nodes, "-u", "hacluster"],
        input=f"{hacluster_password}\n",
        check=False,
    )


def _completed_successfully(cmd: list[str]) -> bool:
    return _run(cmd, check=False).returncode == 0


def _run_required(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    result = _run(cmd, check=False)
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        detail = output or f"exit status {result.returncode}"
        raise RuntimeError(f"{' '.join(cmd)} failed: {detail}")
    return result


def _apply_drbd_config(resource: str, *, primary: bool = False) -> None:
    if not _completed_successfully(["drbdadm", "dump-md", resource]):
        _run_required(["drbdadm", "create-md", resource])
    if not _completed_successfully(["drbdadm", "status", resource]):
        _run_required(["drbdadm", "up", resource])
    if primary:
        _run_required(["drbdadm", "primary", "--force", resource])


def _validate_drbd_resource_name(resource: str) -> str:
    if not resource:
        raise ValueError("resource cannot be empty")
    if len(resource) > 64:
        raise ValueError("resource must be 64 characters or less")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", resource):
        raise ValueError(
            "resource must start with alphanumeric and contain only "
            "alphanumeric, dots, underscores, or hyphens"
        )
    return resource


def _validate_host_name(host: str, *, field: str) -> str:
    if not host:
        raise ValueError(f"{field} cannot be empty")
    if len(host) > 253:
        raise ValueError(f"{field} must be 253 characters or less")
    labels = host.split(".")
    if any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError(
            f"{field} must contain DNS hostname labels with only "
            "alphanumeric characters and hyphens"
        )
    return host


def _validate_device_path(path: str, *, field: str) -> str:
    if not path:
        raise ValueError(f"{field} cannot be empty")
    parsed = PurePosixPath(path)
    if parsed.parts[:2] != ("/", "dev") or len(parsed.parts) < 3:
        raise ValueError(f"{field} must be an absolute /dev path")
    if any(part in {"", ".", ".."} for part in parsed.parts[2:]):
        raise ValueError(f"{field} must not contain empty or relative path segments")
    if not _DEVICE_PATH_RE.fullmatch(path):
        raise ValueError(f"{field} contains unsupported characters")
    return path


def _validate_replication_ip(value: str, *, field: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as e:
        raise ValueError(f"{field} must be a valid IP address") from e
    if address.version != 4:
        raise ValueError(f"{field} must be an IPv4 address")
    return str(address)


def _validate_drbd_port(port: int) -> int:
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def _render_drbd_config(
    *,
    resource: str,
    device: str,
    disk: str,
    node1: str,
    node1_ip: str,
    node2: str,
    node2_ip: str,
    port: int,
) -> str:
    resource = _validate_drbd_resource_name(resource)
    device = _validate_device_path(device, field="device")
    disk = _validate_device_path(disk, field="disk")
    node1 = _validate_host_name(node1, field="node1")
    node2 = _validate_host_name(node2, field="node2")
    if node1 == node2:
        raise ValueError("node1 and node2 must be different")
    node1_ip = _validate_replication_ip(node1_ip, field="node1_ip")
    node2_ip = _validate_replication_ip(node2_ip, field="node2_ip")
    if node1_ip == node2_ip:
        raise ValueError("node1_ip and node2_ip must be different")
    port = _validate_drbd_port(port)

    return (
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
        True, help="Install /etc/arca-storage/config.toml and api.env if missing"
    ),
):
    """
    Install local resource files (Pacemaker RA, systemd unit files).

    This command is designed to be idempotent.
    """
    try:
        if install_config:
            cfg_dst_dir = Path("/etc/arca-storage")
            cfg_dst_dir.mkdir(parents=True, exist_ok=True)

            config_src = _resource_path("config", "config.toml")
            config_dst = cfg_dst_dir / "config.toml"
            if config_src.exists() and not config_dst.exists():
                shutil.copy2(config_src, config_dst)

            api_env_src = _resource_path("config", "api.env")
            api_env_dst = cfg_dst_dir / "api.env"
            if api_env_src.exists() and not api_env_dst.exists():
                shutil.copy2(api_env_src, api_env_dst)
            if api_env_dst.exists():
                os.chmod(api_env_dst, 0o600)

            # Reload config after installing files so derived env matches.
            cfg = load_settings(DEFAULT_CONFIG_PATH)
        else:
            cfg = load_settings()

        # Pacemaker RA
        ra_src = _resource_path("pacemaker", "NetnsVlan")
        if not ra_src.exists():
            raise RuntimeError(f"Missing packaged RA: {ra_src}")

        ra_dst_dir = Path(f"/usr/lib/ocf/resource.d/{ra_vendor or cfg.cluster.pacemaker_ra_vendor}")
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
            for unit in ["nfs-ganesha@.service", "nfs-ganesha-host@.service"]:
                ganesha_src = _resource_path("systemd", unit)
                ganesha_dst = Path("/etc/systemd/system") / unit
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
    Re-generate /etc/arca-storage/arca-storage.env from config.toml.

    Use this after editing /etc/arca-storage/config.toml.
    """
    try:
        cfg = load_settings()
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
    cfg = load_settings()
    issues: list[str] = []

    def check(cond: bool, ok: str, bad: str) -> None:
        if cond:
            typer.echo(f"OK: {ok}")
        else:
            typer.echo(f"NG: {bad}", err=True)
            issues.append(bad)

    # Config file
    check(DEFAULT_CONFIG_PATH.exists(), f"config present: {DEFAULT_CONFIG_PATH}", f"missing {DEFAULT_CONFIG_PATH}")

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
    ra_path = Path(f"/usr/lib/ocf/resource.d/{cfg.cluster.pacemaker_ra_vendor}/NetnsVlan")
    check(
        ra_path.exists(),
        f"NetnsVlan RA installed at {ra_path}",
        f"missing NetnsVlan RA at {ra_path} (run: arca bootstrap install)",
    )

    # systemd unit files
    check(Path("/etc/systemd/system/nfs-ganesha@.service").exists(), "nfs-ganesha@.service present", "missing nfs-ganesha@.service (run: arca bootstrap install)")
    check(Path("/etc/systemd/system/nfs-ganesha-host@.service").exists(), "nfs-ganesha-host@.service present", "missing nfs-ganesha-host@.service (run: arca bootstrap install)")
    check(Path("/etc/systemd/system/arca-storage-api.service").exists(), "arca-storage-api.service present", "missing arca-storage-api.service (run: arca bootstrap install)")

    # Config sanity (basic)
    check(
        cfg.ganesha.export_dir.startswith("/"),
        f"export_dir={cfg.ganesha.export_dir}",
        f"export_dir must be absolute: {cfg.ganesha.export_dir}",
    )
    check(
        cfg.ganesha.config_dir.startswith("/"),
        f"ganesha_config_dir={cfg.ganesha.config_dir}",
        f"ganesha_config_dir must be absolute: {cfg.ganesha.config_dir}",
    )
    check(bool(cfg.storage.vg_name), f"vg_name={cfg.storage.vg_name}", "vg_name is empty")
    check(bool(cfg.network.parent_interface), f"parent_if={cfg.network.parent_interface}", "parent_if is empty")
    check(bool(cfg.cluster.drbd_resource), f"drbd_resource={cfg.cluster.drbd_resource}", "drbd_resource is empty")

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

            master = f"ms_drbd_{cfg.cluster.drbd_resource}"
            res = _run(["pcs", "resource", "show", master], check=False)
            check(res.returncode == 0, f"Pacemaker DRBD master present: {master}", f"missing Pacemaker DRBD master: {master}")
        else:
            check(False, "pcs available", "pcs not found; cannot verify cluster resources")

        # DRBD status
        if shutil.which("drbdadm"):
            res = _run(["drbdadm", "status", cfg.cluster.drbd_resource], check=False)
            check(
                res.returncode == 0,
                f"drbdadm status ok: {cfg.cluster.drbd_resource}",
                f"drbdadm status failed for {cfg.cluster.drbd_resource}: {(res.stderr or res.stdout).strip()}",
            )
        else:
            check(False, "drbdadm available", "drbdadm not found; cannot verify DRBD")

        # LVM status
        if shutil.which("vgs") and shutil.which("lvs"):
            res = _run(["vgs", cfg.storage.vg_name], check=False)
            check(res.returncode == 0, f"VG present: {cfg.storage.vg_name}", f"missing VG: {cfg.storage.vg_name}")
            res = _run(["lvs", f"{cfg.storage.vg_name}/{cfg.storage.thinpool_name}"], check=False)
            check(
                res.returncode == 0,
                f"Thin pool present: {cfg.storage.vg_name}/{cfg.storage.thinpool_name}",
                f"missing thin pool: {cfg.storage.vg_name}/{cfg.storage.thinpool_name}",
            )
        else:
            check(False, "lvm tools available", "vgs/lvs not found; cannot verify LVM")

        # Directories
        check(
            Path(cfg.ganesha.export_dir).exists(),
            f"export_dir exists: {cfg.ganesha.export_dir}",
            f"missing export_dir: {cfg.ganesha.export_dir}",
        )
        check(
            Path(cfg.ganesha.config_dir).exists(),
            f"ganesha_config_dir exists: {cfg.ganesha.config_dir}",
            f"missing ganesha_config_dir: {cfg.ganesha.config_dir}",
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
        _run(
            ["chpasswd"],
            input=f"hacluster:{hacluster_password}\n",
        )

        # Authenticate and setup
        auth = _pcs_host_auth(node_list, hacluster_password)
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
        res_content = _render_drbd_config(
            resource=resource,
            device=device,
            disk=disk,
            node1=node1,
            node1_ip=node1_ip,
            node2=node2,
            node2_ip=node2_ip,
            port=port,
        )
        dest_dir = Path("/etc/drbd.d")
        dest_dir.mkdir(parents=True, exist_ok=True)
        res_path = dest_dir / f"{resource}.res"
        res_path.write_text(res_content, encoding="utf-8")
        typer.echo(f"Wrote DRBD resource config: {res_path}")

        if apply:
            _apply_drbd_config(resource, primary=primary)
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
        cfg = load_settings()
        vg = vg or cfg.storage.vg_name
        thinpool = thinpool or cfg.storage.thinpool_name

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
