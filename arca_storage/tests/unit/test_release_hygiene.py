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


def test_ansible_site_rejects_disabled_stonith_without_lab_opt_out(repo_root):
    playbook = (repo_root / "ansible/site.yml").read_text(encoding="utf-8")
    group_vars = (repo_root / "ansible/group_vars/all.yml").read_text(encoding="utf-8")

    assert "pacemaker_enable_stonith | default(false) | bool" in playbook
    assert "pacemaker_allow_stonith_disabled_for_lab | default(false) | bool" in playbook
    assert "pacemaker_allow_stonith_disabled_for_lab: false" in group_vars


def test_csi_controller_manifests_drop_privileges(repo_root):
    controller_manifests = [
        repo_root / "csi-arca-storage/deploy/controller.yaml",
        repo_root / "csi-arca-storage/deploy/controller-statefulset.yaml",
        repo_root / "csi-arca-storage/deploy/kustomize/base/controller-statefulset.yaml",
    ]

    for manifest_path in controller_manifests:
        manifest = manifest_path.read_text(encoding="utf-8")
        assert "seccompProfile:" in manifest
        assert "type: RuntimeDefault" in manifest
        assert manifest.count("runAsNonRoot: true") == 1
        assert manifest.count("runAsUser: 65532") == 1
        assert manifest.count("runAsGroup: 65532") == 1
        assert manifest.count("fsGroup: 65532") == 1
        assert manifest.count("allowPrivilegeEscalation: false") == 5
        assert manifest.count("readOnlyRootFilesystem: true") == 5
        assert manifest.count("- ALL") == 5
