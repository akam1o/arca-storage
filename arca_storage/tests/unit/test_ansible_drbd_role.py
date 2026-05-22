"""Regression tests for the Ansible DRBD role."""


def test_drbd_role_validates_resource_name(repo_root):
    tasks = repo_root / "ansible/roles/drbd/tasks/main.yml"
    content = tasks.read_text(encoding="utf-8")

    assert "Validate DRBD resource name" in content
    assert "drbd_resource_name is string" in content
    assert "(drbd_resource_name | string) is match('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')" in content
    assert "drbd_resource_name must be a single safe path component" in content


def test_drbd_role_uses_argv_for_drbdadm_commands(repo_root):
    tasks = repo_root / "ansible/roles/drbd/tasks/main.yml"
    content = tasks.read_text(encoding="utf-8")

    assert 'ansible.builtin.command: "drbdadm' not in content
    assert content.count("argv:") == 6
    for command in ("dump", "dump-md", "create-md", "status", "up", "primary"):
        assert f"- {command}" in content
