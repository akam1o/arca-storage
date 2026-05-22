"""Regression tests for the Ansible LVM role."""


def test_lvm_role_uses_argv_for_lvm_commands(repo_root):
    tasks = repo_root / "ansible/roles/lvm/tasks/main.yml"
    content = tasks.read_text(encoding="utf-8")

    assert "ansible.builtin.command: >" not in content
    assert "ansible.builtin.command: pvs" not in content
    assert "ansible.builtin.command: pvcreate" not in content
    assert "ansible.builtin.command: lvs" not in content
    assert "ansible.builtin.command: blkid" not in content
    assert content.count("argv:") == 8
    assert "- pvs" in content
    assert "- pvcreate" in content
    assert "- lvcreate" in content
    assert "- mkfs.xfs" in content
