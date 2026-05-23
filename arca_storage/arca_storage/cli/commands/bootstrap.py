"""
Bootstrap commands for initial setup without Ansible.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import stat
import subprocess
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Optional

import typer

from arca_storage.config import (
    DEFAULT_CONFIG_PATH,
    load_settings,
    validate_path_component,
)

app = typer.Typer(help="Bootstrap initial system/cluster configuration")

_DEFAULT_COMMAND_TIMEOUT_SECONDS = 30
_DEVICE_PATH_RE = re.compile(r"/dev/[A-Za-z0-9._/+:-]+")
_HOST_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_LVM_SIZE_RE = re.compile(r"(?P<amount>\d+(?:\.\d+)?)(?P<unit>[bBsSkKmMgGtTpPeE])")
_LVM_PERCENT_SIZE_RE = re.compile(
    r"(?P<amount>\d+(?:\.\d+)?)%(?P<scope>VG|FREE)", re.IGNORECASE
)
_SAFE_OPERATION_SUBCOMMANDS = {
    "drbdadm": 1,
    "pcs": 2,
    "systemctl": 1,
}


def _safe_operation_label(cmd: list[str]) -> str:
    if not cmd:
        return "command"
    command = Path(str(cmd[0])).name or "command"
    parts = [command]
    for token in cmd[1 : 1 + _SAFE_OPERATION_SUBCOMMANDS.get(command, 0)]:
        if token.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.:-]+", token):
            break
        parts.append(token)
    return " ".join(parts)


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
        raise RuntimeError(
            f"{_safe_operation_label(cmd)} timed out after {timeout}s"
        ) from e


def _resource_path(*parts: str) -> Path:
    # arca_storage/cli/commands/bootstrap.py -> arca_storage/resources/...
    return Path(__file__).resolve().parents[2] / "resources" / Path(*parts)


def _render_env(cfg) -> str:
    return cfg.to_systemd_env()


def _validate_path_component(value: str, *, field: str) -> str:
    return validate_path_component(value, field_name=field)


def _validate_cluster_name(cluster_name: str) -> str:
    return _validate_path_component(cluster_name, field="cluster_name")


def _parse_cluster_nodes(nodes: str) -> list[str]:
    node_list = [
        _validate_host_name(node, field="nodes") for node in nodes.split() if node
    ]
    if len(node_list) < 2:
        raise ValueError("Provide at least 2 nodes")
    if len(set(node_list)) != len(node_list):
        raise ValueError("nodes must be unique")
    return node_list


def _pcs_host_auth(
    nodes: list[str],
    hacluster_password: str,
    *,
    timeout: int = _DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    nodes = [_validate_host_name(node, field="nodes") for node in nodes]
    return _run(
        ["pcs", "host", "auth", *nodes, "-u", "hacluster"],
        input=f"{hacluster_password}\n",
        check=False,
        timeout=timeout,
    )


def _completed_successfully(
    cmd: list[str], *, timeout: int = _DEFAULT_COMMAND_TIMEOUT_SECONDS
) -> bool:
    return _run(cmd, check=False, timeout=timeout).returncode == 0


def _run_required(
    cmd: list[str],
    *,
    timeout: int = _DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    result = _run(cmd, check=False, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{_safe_operation_label(cmd)} failed")
    return result


def _apply_drbd_config(
    resource: str,
    *,
    primary: bool = False,
    timeout: int = _DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> None:
    if not _completed_successfully(["drbdadm", "dump-md", resource], timeout=timeout):
        _run_required(["drbdadm", "create-md", resource], timeout=timeout)
    if not _completed_successfully(["drbdadm", "status", resource], timeout=timeout):
        _run_required(["drbdadm", "up", resource], timeout=timeout)
    if primary:
        _run_required(["drbdadm", "primary", "--force", resource], timeout=timeout)


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


def _validate_lvm_size(value: str, *, field: str, allow_percent: bool = False) -> str:
    if not value:
        raise ValueError(f"{field} cannot be empty")

    match = _LVM_SIZE_RE.fullmatch(value)
    if match is None and allow_percent:
        match = _LVM_PERCENT_SIZE_RE.fullmatch(value)
    if match is None:
        unit_hint = " or a percentage such as 80%VG" if allow_percent else ""
        raise ValueError(f"{field} must be a positive LVM size with a unit{unit_hint}")

    try:
        amount = Decimal(match.group("amount"))
    except InvalidOperation as e:
        raise ValueError(f"{field} must be a positive LVM size") from e
    if amount <= 0:
        raise ValueError(f"{field} must be greater than zero")

    if "scope" in match.groupdict() and amount > 100:
        raise ValueError(f"{field} percentage must be 100 or less")

    return value


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


def _ensure_directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True)
        return
    if stat.S_ISLNK(mode):
        raise RuntimeError(f"Refusing to use symlinked directory: {path}")
    if not stat.S_ISDIR(mode):
        raise RuntimeError(f"Refusing to use non-directory path: {path}")


def _ensure_regular_destination(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise RuntimeError(f"Refusing to overwrite symlinked file: {path}")
    if not stat.S_ISREG(mode):
        raise RuntimeError(f"Refusing to overwrite non-regular file: {path}")


def _write_file_atomically(path: Path, content: bytes, *, mode: int) -> None:
    _ensure_directory(path.parent)
    _ensure_regular_destination(path)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _copy_file_atomically(src: Path, dst: Path, *, mode: int) -> None:
    _write_file_atomically(dst, src.read_bytes(), mode=mode)


def _chmod_regular_file(path: Path, mode: int) -> None:
    _ensure_regular_destination(path)
    if path.exists():
        os.chmod(path, mode)


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
    _ensure_directory(env_dst_dir)
    env_dst = env_dst_dir / "arca-storage.env"
    _write_file_atomically(env_dst, _render_env(cfg).encode("utf-8"), mode=0o644)
    return env_dst


@app.command()
def install(
    ra_vendor: str = typer.Option(
        "local", help="OCF vendor directory name (default: local)"
    ),
    install_api_service: bool = typer.Option(
        True, help="Install arca-storage-api systemd unit"
    ),
    install_ganesha_unit: bool = typer.Option(
        True, help="Install nfs-ganesha@.service systemd unit"
    ),
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
            _ensure_directory(cfg_dst_dir)

            config_src = _resource_path("config", "config.toml")
            config_dst = cfg_dst_dir / "config.toml"
            if config_src.exists() and not config_dst.exists():
                _copy_file_atomically(config_src, config_dst, mode=0o644)
            else:
                _ensure_regular_destination(config_dst)

            api_env_src = _resource_path("config", "api.env")
            api_env_dst = cfg_dst_dir / "api.env"
            if api_env_src.exists() and not api_env_dst.exists():
                _copy_file_atomically(api_env_src, api_env_dst, mode=0o600)
            else:
                _chmod_regular_file(api_env_dst, 0o600)

            # Reload config after installing files so derived env matches.
            cfg = load_settings(DEFAULT_CONFIG_PATH)
        else:
            cfg = load_settings()

        # Pacemaker RA
        ra_src = _resource_path("pacemaker", "NetnsVlan")
        if not ra_src.exists():
            raise RuntimeError(f"Missing packaged RA: {ra_src}")

        vendor = _validate_path_component(
            ra_vendor or cfg.cluster.pacemaker_ra_vendor, field="ra_vendor"
        )
        ra_dst_dir = Path("/usr/lib/ocf/resource.d") / vendor
        _ensure_directory(ra_dst_dir)
        ra_dst = ra_dst_dir / "NetnsVlan"
        _copy_file_atomically(ra_src, ra_dst, mode=0o755)

        # systemd units
        if install_api_service:
            api_src = _resource_path("systemd", "arca-storage-api.service")
            api_dst = Path("/etc/systemd/system/arca-storage-api.service")
            if api_src.exists():
                _copy_file_atomically(api_src, api_dst, mode=0o644)

        if install_ganesha_unit:
            for unit in ["nfs-ganesha@.service", "nfs-ganesha-host@.service"]:
                ganesha_src = _resource_path("systemd", unit)
                ganesha_dst = Path("/etc/systemd/system") / unit
                if not ganesha_src.exists():
                    raise RuntimeError(f"Missing packaged systemd unit: {ganesha_src}")
                _copy_file_atomically(ganesha_src, ganesha_dst, mode=0o644)

        # systemd environment file (used by nfs-ganesha@.service)
        _write_env_file(cfg)

        _run(["systemctl", "daemon-reload"], timeout=cfg.timeouts.subprocess_default)
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
        _run(
            ["systemctl", "daemon-reload"],
            check=False,
            timeout=cfg.timeouts.subprocess_default,
        )
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def verify(
    strict: bool = typer.Option(False, help="Exit non-zero if any checks fail"),
    check_system: bool = typer.Option(
        True, help="Run system/cluster status checks (pcs/drbd/lvm/systemd)"
    ),
):
    """
    Verify prerequisites and installed files for bootstrap/runtime.

    This command is non-destructive.
    """
    cfg = load_settings()
    default_timeout = cfg.timeouts.subprocess_default
    pacemaker_timeout = cfg.timeouts.pacemaker_operation
    issues: list[str] = []

    def check(cond: bool, ok: str, bad: str) -> None:
        if cond:
            typer.echo(f"OK: {ok}")
        else:
            typer.echo(f"NG: {bad}", err=True)
            issues.append(bad)

    # Config file
    check(
        DEFAULT_CONFIG_PATH.exists(),
        f"config present: {DEFAULT_CONFIG_PATH}",
        f"missing {DEFAULT_CONFIG_PATH}",
    )

    # systemd env
    check(
        Path("/etc/arca-storage/arca-storage.env").exists(),
        "arca-storage.env present",
        "missing arca-storage.env (run: arca bootstrap render-env)",
    )

    # Key binaries (presence only)
    for binary in [
        "systemctl",
        "pcs",
        "drbdadm",
        "pvcreate",
        "vgcreate",
        "lvcreate",
        "ganesha.nfsd",
        "ip",
    ]:
        check(
            shutil.which(binary) is not None,
            f"found binary: {binary}",
            f"missing binary in PATH: {binary}",
        )

    # Pacemaker RA
    vendor = _validate_path_component(
        cfg.cluster.pacemaker_ra_vendor, field="cluster.pacemaker_ra_vendor"
    )
    ra_path = Path("/usr/lib/ocf/resource.d") / vendor / "NetnsVlan"
    check(
        ra_path.exists(),
        f"NetnsVlan RA installed at {ra_path}",
        f"missing NetnsVlan RA at {ra_path} (run: arca bootstrap install)",
    )

    # systemd unit files
    check(
        Path("/etc/systemd/system/nfs-ganesha@.service").exists(),
        "nfs-ganesha@.service present",
        "missing nfs-ganesha@.service (run: arca bootstrap install)",
    )
    check(
        Path("/etc/systemd/system/nfs-ganesha-host@.service").exists(),
        "nfs-ganesha-host@.service present",
        "missing nfs-ganesha-host@.service (run: arca bootstrap install)",
    )
    check(
        Path("/etc/systemd/system/arca-storage-api.service").exists(),
        "arca-storage-api.service present",
        "missing arca-storage-api.service (run: arca bootstrap install)",
    )

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
    check(
        bool(cfg.storage.vg_name), f"vg_name={cfg.storage.vg_name}", "vg_name is empty"
    )
    check(
        bool(cfg.network.parent_interface),
        f"parent_if={cfg.network.parent_interface}",
        "parent_if is empty",
    )
    check(
        bool(cfg.cluster.drbd_resource),
        f"drbd_resource={cfg.cluster.drbd_resource}",
        "drbd_resource is empty",
    )
    check(
        bool(cfg.csi.client_cidrs),
        f"csi.client_cidrs={','.join(cfg.csi.client_cidrs)}",
        "csi.client_cidrs is empty; set Kubernetes node CIDRs before using CSI directory volumes",
    )

    if check_system:
        # systemd health (only if systemctl exists)
        if shutil.which("systemctl"):
            for unit in ["pcsd", "corosync", "pacemaker"]:
                res = _run(
                    ["systemctl", "is-active", unit],
                    check=False,
                    timeout=default_timeout,
                )
                check(
                    res.returncode == 0,
                    f"systemd {unit} is active",
                    f"systemd {unit} is not active",
                )
        else:
            check(
                False,
                "systemctl available",
                "systemctl not found; cannot verify services",
            )

        # Pacemaker cluster health
        if shutil.which("pcs"):
            res = _run(["pcs", "status"], check=False, timeout=pacemaker_timeout)
            check(res.returncode == 0, "pcs status ok", "pcs status failed")

            master = f"ms_drbd_{cfg.cluster.drbd_resource}"
            res = _run(
                ["pcs", "resource", "show", master],
                check=False,
                timeout=pacemaker_timeout,
            )
            check(
                res.returncode == 0,
                f"Pacemaker DRBD master present: {master}",
                f"missing Pacemaker DRBD master: {master}",
            )
        else:
            check(
                False, "pcs available", "pcs not found; cannot verify cluster resources"
            )

        # DRBD status
        if shutil.which("drbdadm"):
            res = _run(
                ["drbdadm", "status", cfg.cluster.drbd_resource],
                check=False,
                timeout=default_timeout,
            )
            check(
                res.returncode == 0,
                f"drbdadm status ok: {cfg.cluster.drbd_resource}",
                "drbdadm status failed",
            )
        else:
            check(False, "drbdadm available", "drbdadm not found; cannot verify DRBD")

        # LVM status
        if shutil.which("vgs") and shutil.which("lvs"):
            res = _run(
                ["vgs", cfg.storage.vg_name], check=False, timeout=default_timeout
            )
            check(
                res.returncode == 0,
                f"VG present: {cfg.storage.vg_name}",
                f"missing VG: {cfg.storage.vg_name}",
            )
            res = _run(
                ["lvs", f"{cfg.storage.vg_name}/{cfg.storage.thinpool_name}"],
                check=False,
                timeout=default_timeout,
            )
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
    hacluster_password: str = typer.Option(
        ..., prompt=True, hide_input=True, confirmation_prompt=True
    ),
    stonith_enabled: bool = typer.Option(False, help="Set stonith-enabled property"),
):
    """
    Bootstrap Pacemaker/Corosync cluster using pcs.

    This runs locally and configures the cluster across the provided nodes.
    """
    try:
        cluster_name = _validate_cluster_name(cluster_name)
        node_list = _parse_cluster_nodes(nodes)
        cfg = load_settings(require_file=False)
        default_timeout = cfg.timeouts.subprocess_default
        pacemaker_timeout = cfg.timeouts.pacemaker_operation

        # Ensure pcsd is running
        _run(["systemctl", "enable", "--now", "pcsd"], timeout=default_timeout)

        # Ensure hacluster password
        _run(
            ["chpasswd"],
            input=f"hacluster:{hacluster_password}\n",
            timeout=default_timeout,
        )

        # Authenticate and setup
        auth = _pcs_host_auth(node_list, hacluster_password, timeout=pacemaker_timeout)
        if auth.returncode != 0 and "Authorized" not in (auth.stdout or ""):
            raise RuntimeError("pcs host auth failed")

        if not Path("/etc/corosync/authkey").exists():
            setup = _run(
                ["pcs", "cluster", "setup", "--name", cluster_name, *node_list],
                check=False,
                timeout=pacemaker_timeout,
            )
            if (
                setup.returncode != 0
                and "already exists" not in (setup.stderr or "").lower()
            ):
                raise RuntimeError("pcs cluster setup failed")

        _run(["pcs", "cluster", "start", "--all"], timeout=pacemaker_timeout)
        _run(["pcs", "cluster", "enable", "--all"], timeout=pacemaker_timeout)

        stonith_value = "true" if stonith_enabled else "false"
        _run(
            ["pcs", "property", "set", f"stonith-enabled={stonith_value}"],
            timeout=pacemaker_timeout,
        )

        typer.echo("Pacemaker cluster bootstrap completed")
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def drbd_config(
    resource: str = typer.Option("r0", help="DRBD resource name (default: r0)"),
    device: str = typer.Option(
        "/dev/drbd0", help="DRBD device path (default: /dev/drbd0)"
    ),
    disk: str = typer.Option(..., help="Backing disk/partition (e.g., /dev/nvme0n1p1)"),
    node1: str = typer.Option(..., help="Node1 hostname (matches uname/pcs)"),
    node1_ip: str = typer.Option(..., help="Node1 replication IP"),
    node2: str = typer.Option(..., help="Node2 hostname (matches uname/pcs)"),
    node2_ip: str = typer.Option(..., help="Node2 replication IP"),
    port: int = typer.Option(7788, help="Replication port (default: 7788)"),
    apply: bool = typer.Option(
        False, help="Run drbdadm create-md/up after writing config"
    ),
    primary: bool = typer.Option(
        False, help="Promote this node to primary (requires --apply)"
    ),
):
    """
    Write DRBD resource configuration to /etc/drbd.d/<resource>.res.
    """
    try:
        cfg = load_settings(require_file=False)
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
        _ensure_directory(dest_dir)
        res_path = dest_dir / f"{resource}.res"
        _write_file_atomically(res_path, res_content.encode("utf-8"), mode=0o644)
        typer.echo(f"Wrote DRBD resource config: {res_path}")

        if apply:
            _apply_drbd_config(
                resource, primary=primary, timeout=cfg.timeouts.subprocess_default
            )
            typer.echo("Applied DRBD configuration")
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def lvm_thinpool(
    pv: str = typer.Option("/dev/drbd0", help="PV device path (default: /dev/drbd0)"),
    vg: Optional[str] = typer.Option(
        None, help="Volume group name (default: from config or vg_pool_01)"
    ),
    thinpool: Optional[str] = typer.Option(
        None, help="Thin pool LV name (default: from config or pool)"
    ),
    size: str = typer.Option(
        "80%VG", help="Thin pool size for lvcreate -L (default: 80%VG)"
    ),
    metadata_size: str = typer.Option(
        "15.8G", help="Thin pool metadata size (default: 15.8G)"
    ),
    chunk_size: str = typer.Option("256K", help="Thin pool chunk size (default: 256K)"),
):
    """
    Create PV/VG/thinpool required by arca on the local node.
    """
    try:
        cfg = load_settings()
        timeout = cfg.timeouts.subprocess_default
        pv = _validate_device_path(pv, field="pv")
        vg = _validate_path_component(vg or cfg.storage.vg_name, field="vg")
        thinpool = _validate_path_component(
            thinpool or cfg.storage.thinpool_name, field="thinpool"
        )
        size = _validate_lvm_size(size, field="size", allow_percent=True)
        metadata_size = _validate_lvm_size(metadata_size, field="metadata_size")
        chunk_size = _validate_lvm_size(chunk_size, field="chunk_size")

        # PV
        pv_check = _run(["pvs", pv], check=False, timeout=timeout)
        if pv_check.returncode != 0:
            _run(["pvcreate", pv], timeout=timeout)

        # VG
        vg_check = _run(["vgs", vg], check=False, timeout=timeout)
        if vg_check.returncode != 0:
            _run(["vgcreate", vg, pv], timeout=timeout)

        # Thinpool
        lv_path = f"{vg}/{thinpool}"
        lv_check = _run(["lvs", lv_path], check=False, timeout=timeout)
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
                ],
                timeout=timeout,
            )

        _run(
            ["systemctl", "enable", "--now", "lvm2-monitor"],
            check=False,
            timeout=timeout,
        )
        typer.echo("LVM thin pool bootstrap completed")
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
