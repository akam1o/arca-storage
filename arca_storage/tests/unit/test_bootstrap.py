import subprocess
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


def test_apply_drbd_config_runs_missing_steps():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:2] == ["drbdadm", "dump-md"]:
            return completed(cmd, 1, stderr="no metadata")
        if cmd[:2] == ["drbdadm", "status"]:
            return completed(cmd, 1, stderr="not up")
        return completed(cmd)

    with patch.object(bootstrap, "_run", side_effect=fake_run):
        bootstrap._apply_drbd_config("r0", primary=True)

    assert [cmd for cmd, _kwargs in calls] == [
        ["drbdadm", "dump-md", "r0"],
        ["drbdadm", "create-md", "r0"],
        ["drbdadm", "status", "r0"],
        ["drbdadm", "up", "r0"],
        ["drbdadm", "primary", "--force", "r0"],
    ]
    assert all(kwargs["check"] is False for _cmd, kwargs in calls)


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
