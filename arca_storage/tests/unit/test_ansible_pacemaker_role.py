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


def test_pacemaker_resources_use_argv_for_pcs_commands(repo_root):
    resources = repo_root / "ansible/roles/pacemaker/tasks/resources.yml"
    content = resources.read_text(encoding="utf-8")

    assert "ansible.builtin.command: >" not in content
    assert "ansible.builtin.command: pcs" not in content
    assert content.count("argv:") == 15
    assert "drbd_resource={{ drbd_resource_name }}" in content
    assert "device={{ pacemaker_fs_device | default" in content
    assert "systemd:nfs-ganesha@{{ pacemaker_ganesha_instance" in content


def test_pacemaker_resources_validate_variable_inputs(repo_root):
    resources = repo_root / "ansible/roles/pacemaker/tasks/resources.yml"
    content = resources.read_text(encoding="utf-8")

    assert "Validate Pacemaker resource inputs" in content
    assert "pacemaker_fs_device | default" in content
    assert "^/dev/" in content
    assert "pacemaker_fs_directory | default('/exports/tenant_a')" in content
    assert "pacemaker_netnsvlan_vlan_id | default(100)" in content
    assert "pacemaker_netnsvlan_ip | default('192.168.10.5')" in content
    assert "pacemaker_netnsvlan_mtu | default(9000)" in content
    assert "pacemaker_ganesha_instance | default('tenant_a')" in content
    assert "safe systemd instance names" in content
