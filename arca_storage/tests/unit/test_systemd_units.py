"""Regression tests for packaged systemd unit files."""


def _ganesha_service_files(repo_root):
    return [
        repo_root / "arca_storage/arca_storage/resources/systemd/nfs-ganesha@.service",
        repo_root / "arca_storage/arca_storage/resources/systemd/nfs-ganesha-host@.service",
        repo_root / "ansible/roles/nfs-ganesha/templates/nfs-ganesha.service.j2",
        repo_root / "ansible/roles/nfs-ganesha/templates/nfs-ganesha-host.service.j2",
    ]


def test_ganesha_units_pass_configured_pid_file(repo_root):
    for service_file in _ganesha_service_files(repo_root):
        content = service_file.read_text(encoding="utf-8")

        assert "PIDFile=/var/run/ganesha.%i.pid" in content
        assert "-p /var/run/ganesha.%i.pid" in content


def test_ganesha_units_use_configured_config_dir(repo_root):
    for service_file in _ganesha_service_files(repo_root):
        content = service_file.read_text(encoding="utf-8")

        assert "EnvironmentFile=-/etc/arca-storage/arca-storage.env" in content
        assert "${ARCA_GANESHA_CONFIG_DIR}/ganesha.%i.conf" in content
        assert "/etc/ganesha/ganesha.%i.conf" not in content


def test_ansible_netnsvlan_uses_packaged_resource_agent(repo_root):
    role_copy = repo_root / "ansible/roles/pacemaker/files/NetnsVlan"
    defaults = (repo_root / "ansible/roles/pacemaker/defaults/main.yml").read_text(encoding="utf-8")
    tasks = (repo_root / "ansible/roles/pacemaker/tasks/main.yml").read_text(encoding="utf-8")

    assert not role_copy.exists()
    assert "arca_storage/arca_storage/resources/pacemaker/NetnsVlan" in defaults
    assert "src: \"{{ pacemaker_netnsvlan_ra_src }}\"" in tasks
