"""Regression tests for packaged systemd unit files."""


def _ganesha_service_files(repo_root):
    return [
        repo_root / "arca_storage/arca_storage/resources/systemd/nfs-ganesha@.service",
        repo_root
        / "arca_storage/arca_storage/resources/systemd/nfs-ganesha-host@.service",
        repo_root / "ansible/roles/nfs-ganesha/templates/nfs-ganesha.service.j2",
        repo_root / "ansible/roles/nfs-ganesha/templates/nfs-ganesha-host.service.j2",
    ]


def _api_service_file(repo_root):
    return (
        repo_root
        / "arca_storage/arca_storage/resources/systemd/arca-storage-api.service"
    )


def test_api_unit_has_baseline_hardening(repo_root):
    content = _api_service_file(repo_root).read_text(encoding="utf-8")

    for directive in [
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=full",
        "ProtectHome=true",
        "LockPersonality=true",
        "RestrictRealtime=true",
        "RestrictSUIDSGID=true",
        "ProtectClock=true",
        "ProtectHostname=true",
        "ProtectKernelLogs=true",
        "SystemCallArchitectures=native",
    ]:
        assert directive in content

    assert (
        "ReadWritePaths=/etc/arca-storage /etc/ganesha /var/lib/arca-storage /run /var/run /exports"
        in content
    )
    assert "CapabilityBoundingSet=" in content
    assert "CAP_SYS_ADMIN" in content
    assert "CAP_NET_ADMIN" in content


def test_api_unit_uses_packaged_config_for_server_settings(repo_root):
    content = _api_service_file(repo_root).read_text(encoding="utf-8")

    assert "Environment=ARCA_CONFIG_PATH=/etc/arca-storage/config.toml" in content
    assert "ExecStart=/usr/bin/arca-storage-api" in content
    assert "--ssl-certfile" not in content
    assert "--ssl-keyfile" not in content


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
    defaults = (repo_root / "ansible/roles/pacemaker/defaults/main.yml").read_text(
        encoding="utf-8"
    )
    tasks = (repo_root / "ansible/roles/pacemaker/tasks/main.yml").read_text(
        encoding="utf-8"
    )

    assert not role_copy.exists()
    assert "arca_storage/arca_storage/resources/pacemaker/NetnsVlan" in defaults
    assert 'src: "{{ pacemaker_netnsvlan_ra_src }}"' in tasks


def test_ansible_pacemaker_validates_ra_vendor_path_component(repo_root):
    defaults = (repo_root / "ansible/roles/pacemaker/defaults/main.yml").read_text(
        encoding="utf-8"
    )
    tasks = (repo_root / "ansible/roles/pacemaker/tasks/main.yml").read_text(
        encoding="utf-8"
    )

    assert "pacemaker_ra_vendor: local" in defaults
    assert "Validate Pacemaker RA vendor" in tasks
    assert "pacemaker_ra_vendor is string" in tasks
    assert (
        "(pacemaker_ra_vendor | string) is match('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')"
        in tasks
    )
    assert "pacemaker_ra_vendor must be a single safe path component" in tasks
