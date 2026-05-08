"""Regression tests for packaged systemd unit files."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]

GANESHA_SERVICE_FILES = [
    ROOT / "arca_storage/arca_storage/resources/systemd/nfs-ganesha@.service",
    ROOT / "arca_storage/arca_storage/resources/systemd/nfs-ganesha-host@.service",
    ROOT / "ansible/roles/nfs-ganesha/templates/nfs-ganesha.service.j2",
    ROOT / "ansible/roles/nfs-ganesha/templates/nfs-ganesha-host.service.j2",
]


def test_ganesha_units_pass_configured_pid_file():
    for service_file in GANESHA_SERVICE_FILES:
        content = service_file.read_text(encoding="utf-8")

        assert "PIDFile=/var/run/ganesha.%i.pid" in content
        assert "-p /var/run/ganesha.%i.pid" in content


def test_ganesha_units_use_configured_config_dir():
    for service_file in GANESHA_SERVICE_FILES:
        content = service_file.read_text(encoding="utf-8")

        assert "EnvironmentFile=-/etc/arca-storage/arca-storage.env" in content
        assert "${ARCA_GANESHA_CONFIG_DIR}/ganesha.%i.conf" in content
        assert "/etc/ganesha/ganesha.%i.conf" not in content


def test_ansible_netnsvlan_uses_packaged_resource_agent():
    role_copy = ROOT / "ansible/roles/pacemaker/files/NetnsVlan"
    defaults = (ROOT / "ansible/roles/pacemaker/defaults/main.yml").read_text(encoding="utf-8")
    tasks = (ROOT / "ansible/roles/pacemaker/tasks/main.yml").read_text(encoding="utf-8")

    assert not role_copy.exists()
    assert "arca_storage/arca_storage/resources/pacemaker/NetnsVlan" in defaults
    assert "src: \"{{ pacemaker_netnsvlan_ra_src }}\"" in tasks
