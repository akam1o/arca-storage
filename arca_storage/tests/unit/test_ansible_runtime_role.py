"""Regression tests for Ansible CLI command snippets."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUNTIME_TASKS = ROOT / "ansible/roles/arca-runtime/tasks/main.yml"


def test_runtime_role_uses_current_export_access_option():
    content = RUNTIME_TASKS.read_text(encoding="utf-8")

    assert "--access {{ item.access }}" in content
    assert "{% if item.access == 'ro' %}--ro{% else %}--rw{% endif %}" not in content


def test_runtime_role_allows_host_network_svms():
    content = RUNTIME_TASKS.read_text(encoding="utf-8")

    assert (
        "{% if item.vlan_id is defined and item.vlan_id is not none %}"
        "--vlan {{ item.vlan_id }}{% endif %}"
    ) in content
