import subprocess
from unittest.mock import patch

from arca_storage.cli.commands import bootstrap


def test_pcs_host_auth_does_not_put_password_in_argv():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch.object(bootstrap.subprocess, "run", return_value=completed) as run:
        result = bootstrap._pcs_host_auth(["node-a", "node-b"], "secret-password")

    assert result is completed
    cmd = run.call_args.args[0]
    assert cmd == ["pcs", "host", "auth", "node-a", "node-b", "-u", "hacluster"]
    assert all("secret-password" not in arg for arg in cmd)
    assert run.call_args.kwargs["input"] == "secret-password\n"
    assert run.call_args.kwargs["check"] is False
