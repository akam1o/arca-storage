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
