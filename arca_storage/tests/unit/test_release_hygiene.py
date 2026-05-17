"""Regression tests for release and CI guardrails."""


def test_top_level_ci_runs_on_main_and_develop(repo_root):
    workflow = (repo_root / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "branches: [main, develop]" in workflow
    assert "branches: [main]" not in workflow


def test_csi_runtime_image_uses_supported_alpine_branch(repo_root):
    dockerfile = (repo_root / "csi-arca-storage/Dockerfile").read_text(encoding="utf-8")

    assert "FROM alpine:3.23" in dockerfile
    assert "FROM alpine:3.19" not in dockerfile


def test_ansible_site_rejects_default_cluster_secrets(repo_root):
    playbook = (repo_root / "ansible/site.yml").read_text(encoding="utf-8")

    assert 'pacemaker_hacluster_password != "changeme"' in playbook
    assert 'drbd_shared_secret != "changeme"' in playbook
