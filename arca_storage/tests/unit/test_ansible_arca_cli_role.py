"""Regression tests for the Ansible arca-cli role."""


def test_arca_cli_url_install_requires_checksum_or_explicit_opt_out(repo_root):
    tasks = repo_root / "ansible/roles/arca-cli/tasks/main.yml"
    content = tasks.read_text(encoding="utf-8")

    assert "arca_cli_download_checksum is required when using url method" in content
    assert "arca_cli_download_checksum | default('') | trim | length > 0" in content
    assert "arca_cli_allow_unverified_download | default(false) | bool" in content
    assert "default(omit)" not in content


def test_arca_cli_unverified_downloads_are_disabled_by_default(repo_root):
    group_vars = repo_root / "ansible/group_vars/all.yml"
    content = group_vars.read_text(encoding="utf-8")

    assert 'arca_cli_download_checksum: ""' in content
    assert "arca_cli_allow_unverified_download: false" in content
