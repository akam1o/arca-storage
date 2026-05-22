"""Regression tests for the Ansible Pacemaker role."""


def test_pacemaker_role_validates_cluster_identifiers(repo_root):
    tasks = repo_root / "ansible/roles/pacemaker/tasks/main.yml"
    content = tasks.read_text(encoding="utf-8")

    assert "Validate Pacemaker cluster identifiers" in content
    assert "pacemaker_cluster_name is string" in content
    assert "(pacemaker_cluster_name | string) is match('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')" in content
    assert "(pacemaker_nodes | length) >= 2" in content
    assert "must contain at least two unique DNS hostnames" in content


def test_pacemaker_role_uses_argv_for_bootstrap_pcs_commands(repo_root):
    tasks = repo_root / "ansible/roles/pacemaker/tasks/main.yml"
    content = tasks.read_text(encoding="utf-8")

    assert "pcs host auth {{ pacemaker_nodes" not in content
    assert "pcs cluster setup --name {{ pacemaker_cluster_name }}" not in content
    assert "pcs property set stonith-enabled={{" not in content
    assert content.count("argv:") >= 6
    assert "['pcs', 'host', 'auth']" in content
    assert "['pcs', 'cluster', 'setup', '--name', pacemaker_cluster_name]" in content
    assert '"stonith-enabled={{ pacemaker_enable_stonith' in content
