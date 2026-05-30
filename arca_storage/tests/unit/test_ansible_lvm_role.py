"""Regression tests for the Ansible LVM role."""


def test_lvm_role_validates_inputs(repo_root):
    tasks = repo_root / "ansible/roles/lvm/tasks/main.yml"
    content = tasks.read_text(encoding="utf-8")

    assert "Validate LVM inputs" in content
    assert "lvm_pv_devices is not string" in content
    assert "^/dev/[A-Za-z0-9._/+:-]+$" in content
    assert "lvm_vg_name | string" in content
    assert "lvm_thinpool_name | string" in content
    assert "lvm_thin_volume_name | default('vol_tenant_a')" in content
    assert "LVM inputs must use absolute /dev paths" in content


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
