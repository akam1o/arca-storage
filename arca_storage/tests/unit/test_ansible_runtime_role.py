"""Regression tests for Ansible CLI command snippets."""


def test_runtime_role_uses_current_export_access_option(repo_root):
    runtime_tasks = repo_root / "ansible/roles/arca-runtime/tasks/main.yml"
    content = runtime_tasks.read_text(encoding="utf-8")

    assert "'--access', item.access" in content
    assert "{% if item.access == 'ro' %}--ro{% else %}--rw{% endif %}" not in content


def test_runtime_role_allows_host_network_svms(repo_root):
    runtime_tasks = repo_root / "ansible/roles/arca-runtime/tasks/main.yml"
    content = runtime_tasks.read_text(encoding="utf-8")

    assert (
        "(item.vlan_id is defined and item.vlan_id is not none) | "
        "ternary(['--vlan', item.vlan_id | string], [])"
    ) in content


def test_runtime_role_uses_argv_for_arca_commands(repo_root):
    runtime_tasks = repo_root / "ansible/roles/arca-runtime/tasks/main.yml"
    content = runtime_tasks.read_text(encoding="utf-8")

    assert "ansible.builtin.command: >-" not in content
    assert content.count("argv:") == 4
    assert "'svm', 'create'" in content
    assert "'volume', 'create'" in content
    assert "'export', 'add'" in content


def test_runtime_role_validates_provisioning_inputs(repo_root):
    runtime_tasks = repo_root / "ansible/roles/arca-runtime/tasks/main.yml"
    content = runtime_tasks.read_text(encoding="utf-8")

    assert "Validate arca runtime settings" in content
    assert "Validate arca runtime SVM inputs" in content
    assert "Validate arca runtime volume inputs" in content
    assert "Validate arca runtime export inputs" in content
    assert "arca_runtime_svms is sequence" in content
    assert "arca_runtime_volumes is sequence" in content
    assert "arca_runtime_exports is sequence" in content
    assert "arca_cli_bin_path | default('/usr/bin/arca')" in content
    assert "item.ip_cidr | string" in content
    assert "item.vlan_id | int) <= 4094" in content
    assert "item.mtu is not defined or item.mtu is none" in content
    assert "item.size_gib | string" in content
    assert "item.client | string" in content
    assert "item.access in ['rw', 'ro']" in content
