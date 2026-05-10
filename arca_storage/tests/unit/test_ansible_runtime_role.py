"""Regression tests for Ansible CLI command snippets."""


def test_runtime_role_uses_current_export_access_option(repo_root):
    runtime_tasks = repo_root / "ansible/roles/arca-runtime/tasks/main.yml"
    content = runtime_tasks.read_text(encoding="utf-8")

    assert "--access {{ item.access }}" in content
    assert "{% if item.access == 'ro' %}--ro{% else %}--rw{% endif %}" not in content


def test_runtime_role_allows_host_network_svms(repo_root):
    runtime_tasks = repo_root / "ansible/roles/arca-runtime/tasks/main.yml"
    content = runtime_tasks.read_text(encoding="utf-8")

    assert (
        "{% if item.vlan_id is defined and item.vlan_id is not none %}"
        "--vlan {{ item.vlan_id }}{% endif %}"
    ) in content
