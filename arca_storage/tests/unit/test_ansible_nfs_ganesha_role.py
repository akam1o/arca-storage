"""Regression tests for the Ansible NFS-Ganesha role."""


def test_nfs_ganesha_role_validates_template_inputs(repo_root):
    tasks = repo_root / "ansible/roles/nfs-ganesha/tasks/main.yml"
    content = tasks.read_text(encoding="utf-8")

    assert "Validate NFS-Ganesha inputs" in content
    assert "nfs_ganesha_config_dir is string" in content
    assert "nfs_ganesha_export_dir is string" in content
    assert "nfs_ganesha_export_id | string" in content
    assert "nfs_ganesha_protocols is not string" in content
    assert "nfs_ganesha_transports is not string" in content
    assert "nfs_ganesha_tenants is not string" in content
    assert "nfs_ganesha_export_clients | string" in content
    assert "CIDR-only export clients" in content
