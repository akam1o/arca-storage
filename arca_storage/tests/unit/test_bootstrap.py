import subprocess
import stat
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from arca_storage.cli.commands import bootstrap


def completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def test_pcs_host_auth_does_not_put_password_in_argv():
    result_process = completed([])

    with patch.object(bootstrap.subprocess, "run", return_value=result_process) as run:
        result = bootstrap._pcs_host_auth(["node-a", "node-b"], "secret-password")

    assert result is result_process
    cmd = run.call_args.args[0]
    assert cmd == ["pcs", "host", "auth", "node-a", "node-b", "-u", "hacluster"]
    assert all("secret-password" not in arg for arg in cmd)
    assert run.call_args.kwargs["input"] == "secret-password\n"
    assert run.call_args.kwargs["check"] is False
    assert run.call_args.kwargs["timeout"] == bootstrap._DEFAULT_COMMAND_TIMEOUT_SECONDS


def test_run_uses_default_timeout():
    result_process = completed(["systemctl", "status"])

    with patch.object(bootstrap.subprocess, "run", return_value=result_process) as run:
        result = bootstrap._run(["systemctl", "status"])

    assert result is result_process
    assert run.call_args.kwargs["timeout"] == bootstrap._DEFAULT_COMMAND_TIMEOUT_SECONDS


def test_run_raises_runtime_error_on_timeout():
    with patch.object(
        bootstrap.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(["pcs", "status"], timeout=30),
    ):
        with pytest.raises(RuntimeError, match="pcs status timed out after 30s"):
            bootstrap._run(["pcs", "status"])


def test_apply_drbd_config_runs_missing_steps_with_configured_timeout():
    calls = []
    timeout = 120

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:2] == ["drbdadm", "dump-md"]:
            return completed(cmd, 1, stderr="no metadata")
        if cmd[:2] == ["drbdadm", "status"]:
            return completed(cmd, 1, stderr="not up")
        return completed(cmd)

    with patch.object(bootstrap, "_run", side_effect=fake_run):
        bootstrap._apply_drbd_config("r0", primary=True, timeout=timeout)

    assert [cmd for cmd, _kwargs in calls] == [
        ["drbdadm", "dump-md", "r0"],
        ["drbdadm", "create-md", "r0"],
        ["drbdadm", "status", "r0"],
        ["drbdadm", "up", "r0"],
        ["drbdadm", "primary", "--force", "r0"],
    ]
    assert all(kwargs["check"] is False for _cmd, kwargs in calls)
    assert all(kwargs["timeout"] == timeout for _cmd, kwargs in calls)


def test_apply_drbd_config_skips_existing_metadata_and_running_resource():
    with patch.object(
        bootstrap,
        "_run",
        side_effect=[
            completed(["drbdadm", "dump-md", "r0"]),
            completed(["drbdadm", "status", "r0"]),
        ],
    ) as run:
        bootstrap._apply_drbd_config("r0", primary=False)

    assert [call.args[0] for call in run.call_args_list] == [
        ["drbdadm", "dump-md", "r0"],
        ["drbdadm", "status", "r0"],
    ]


def test_apply_drbd_config_raises_on_create_md_failure():
    with patch.object(
        bootstrap,
        "_run",
        side_effect=[
            completed(["drbdadm", "dump-md", "r0"], 1),
            completed(["drbdadm", "create-md", "r0"], 20, stderr="bad disk"),
        ],
    ):
        with pytest.raises(RuntimeError, match="create-md.*bad disk"):
            bootstrap._apply_drbd_config("r0")


def test_render_drbd_config_validates_and_renders():
    rendered = bootstrap._render_drbd_config(
        resource="r0",
        device="/dev/drbd0",
        disk="/dev/nvme0n1p1",
        node1="arca-node-1.example.local",
        node1_ip="192.0.2.10",
        node2="arca-node-2.example.local",
        node2_ip="192.0.2.11",
        port=7788,
    )

    assert "resource r0 {" in rendered
    assert "on arca-node-1.example.local {" in rendered
    assert "device /dev/drbd0;" in rendered
    assert "disk /dev/nvme0n1p1;" in rendered
    assert "address 192.0.2.10:7788;" in rendered


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"resource": "r0\nresource evil"}, "resource"),
        ({"device": "/tmp/drbd0"}, "device"),
        ({"disk": "/dev/../etc/passwd"}, "disk"),
        ({"node1": "node1; bad"}, "node1"),
        ({"node1_ip": "not-an-ip"}, "node1_ip"),
        ({"node2_ip": "2001:db8::1"}, "node2_ip"),
        ({"port": 0}, "port"),
        ({"node2": "node-a"}, "node1 and node2"),
        ({"node2_ip": "192.0.2.10"}, "node1_ip and node2_ip"),
    ],
)
def test_render_drbd_config_rejects_invalid_values(overrides, message):
    kwargs = {
        "resource": "r0",
        "device": "/dev/drbd0",
        "disk": "/dev/nvme0n1p1",
        "node1": "node-a",
        "node1_ip": "192.0.2.10",
        "node2": "node-b",
        "node2_ip": "192.0.2.11",
        "port": 7788,
    }
    kwargs.update(overrides)

    with pytest.raises(ValueError, match=message):
        bootstrap._render_drbd_config(**kwargs)


@pytest.mark.parametrize("vendor", ["../etc", "/tmp", "local/bad", "..", ".hidden", ""])
def test_validate_path_component_rejects_unsafe_ra_vendor(vendor):
    with pytest.raises(ValueError, match="ra_vendor"):
        bootstrap._validate_path_component(vendor, field="ra_vendor")


def test_copy_file_atomically_sets_requested_mode(tmp_path):
    src = tmp_path / "api.env.src"
    src.write_text("ARCA_API_TOKEN=secret\n", encoding="utf-8")
    src.chmod(0o644)
    dst = tmp_path / "api.env"

    bootstrap._copy_file_atomically(src, dst, mode=0o600)

    assert dst.read_text(encoding="utf-8") == "ARCA_API_TOKEN=secret\n"
    assert stat.S_IMODE(dst.lstat().st_mode) == 0o600


def test_copy_file_atomically_refuses_symlink_destination(tmp_path):
    src = tmp_path / "api.env.src"
    src.write_text("ARCA_API_TOKEN=secret\n", encoding="utf-8")
    target = tmp_path / "target"
    target.write_text("unchanged\n", encoding="utf-8")
    dst = tmp_path / "api.env"
    dst.symlink_to(target)

    with pytest.raises(RuntimeError, match="symlinked file"):
        bootstrap._copy_file_atomically(src, dst, mode=0o600)

    assert target.read_text(encoding="utf-8") == "unchanged\n"


def test_lvm_thinpool_uses_configured_subprocess_timeout():
    cfg = SimpleNamespace(
        storage=SimpleNamespace(vg_name="vg_pool_01", thinpool_name="pool"),
        timeouts=SimpleNamespace(subprocess_default=120),
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[0] in {"pvs", "vgs", "lvs"}:
            return completed(cmd, 1)
        return completed(cmd)

    with (
        patch.object(bootstrap, "load_settings", return_value=cfg),
        patch.object(bootstrap, "_run", side_effect=fake_run),
    ):
        bootstrap.lvm_thinpool(
            pv="/dev/drbd0",
            vg=None,
            thinpool=None,
            size="80%VG",
            metadata_size="15.8G",
            chunk_size="256K",
        )

    assert [cmd[0] for cmd, _kwargs in calls] == [
        "pvs",
        "pvcreate",
        "vgs",
        "vgcreate",
        "lvs",
        "lvcreate",
        "systemctl",
    ]
    assert all(kwargs["timeout"] == 120 for _cmd, kwargs in calls)
