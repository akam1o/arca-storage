"""Regression tests for release and CI guardrails."""

import tomli


def test_top_level_ci_runs_on_main_and_develop(repo_root):
    workflow = (repo_root / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "branches: [main, develop]" in workflow
    assert "branches: [main]" not in workflow


def test_python_workflow_checks_lint_and_format(repo_root):
    workflow = (repo_root / ".github/workflows/python-tests.yml").read_text(
        encoding="utf-8"
    )

    assert "name: Python Type, Lint, Format, And Security Gates" in workflow
    assert "python -m ruff check ." in workflow
    assert 'python -m ruff format --check "${python_files[@]}"' in workflow
    assert "git diff --name-only --diff-filter=ACMRT" in workflow
    assert "name: Check Python formatting" in workflow


def test_python_workflow_runs_type_check(repo_root):
    workflow = (repo_root / ".github/workflows/python-tests.yml").read_text(
        encoding="utf-8"
    )

    assert "name: Run type check" in workflow
    assert "python -m mypy arca_storage" in workflow


def test_python_workflow_enforces_coverage_floor(repo_root):
    workflow = (repo_root / ".github/workflows/python-tests.yml").read_text(
        encoding="utf-8"
    )

    assert "--cov-fail-under=50" in workflow


def test_python_slow_tests_run_on_schedule(repo_root):
    workflow = (repo_root / ".github/workflows/python-slow-tests.yml").read_text(
        encoding="utf-8"
    )

    assert "name: Python Slow Tests" in workflow
    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "python -m pytest tests/unit tests/integration -v -m slow" in workflow
    assert "not slow" not in workflow


def test_runtime_dependencies_use_single_requirements_source(repo_root):
    pyproject = tomli.loads(
        (repo_root / "arca_storage/pyproject.toml").read_text(encoding="utf-8")
    )
    vendor_script = (repo_root / "packaging/vendor-wheels.sh").read_text(
        encoding="utf-8"
    )

    assert "dependencies" in pyproject["project"]["dynamic"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["dependencies"] == {
        "file": ["requirements.txt"]
    }
    assert "$ROOT/arca_storage/requirements.txt" in vendor_script
    assert "requirements-runtime.txt" not in vendor_script
    assert not (repo_root / "packaging/requirements-runtime.txt").exists()


def test_csi_runtime_image_uses_supported_alpine_branch(repo_root):
    dockerfile = (repo_root / "csi-arca-storage/Dockerfile").read_text(encoding="utf-8")

    assert "FROM alpine:3.23" in dockerfile
    assert "FROM alpine:3.19" not in dockerfile


def test_csi_driver_version_matches_manifest_tag_and_build_flags(repo_root):
    dockerfile = (repo_root / "csi-arca-storage/Dockerfile").read_text(encoding="utf-8")
    makefile = (repo_root / "csi-arca-storage/Makefile").read_text(encoding="utf-8")
    version_go = (repo_root / "csi-arca-storage/pkg/driver/version.go").read_text(
        encoding="utf-8"
    )
    manifests = [
        repo_root / "csi-arca-storage/deploy/controller.yaml",
        repo_root / "csi-arca-storage/deploy/controller-statefulset.yaml",
        repo_root / "csi-arca-storage/deploy/node.yaml",
        repo_root
        / "csi-arca-storage/deploy/kustomize/base/controller-statefulset.yaml",
        repo_root / "csi-arca-storage/deploy/kustomize/base/node.yaml",
        repo_root / "csi-arca-storage/deploy/kustomize/base/kustomization.yaml",
        repo_root
        / "csi-arca-storage/deploy/kustomize/overlays/production/kustomization.yaml",
    ]

    assert 'DriverVersion = "v1.0.0"' in version_go
    assert "VERSION?=v1.0.0" in makefile
    assert "DOCKER_TAG?=$(VERSION)" in makefile
    assert "-X $(VERSION_PACKAGE).DriverVersion=$(VERSION)" in makefile
    assert "docker build --build-arg VERSION=$(VERSION)" in makefile
    assert "ARG VERSION=v1.0.0" in dockerfile
    assert (
        "-X github.com/akam1o/csi-arca-storage/pkg/driver.DriverVersion=${VERSION}"
        in dockerfile
    )
    for manifest_path in manifests:
        assert "v1.0.0" in manifest_path.read_text(encoding="utf-8")


def test_ansible_site_rejects_default_cluster_secrets(repo_root):
    playbook = (repo_root / "ansible/site.yml").read_text(encoding="utf-8")

    assert 'pacemaker_hacluster_password != "changeme"' in playbook
    assert 'drbd_shared_secret != "changeme"' in playbook


def test_ansible_site_rejects_disabled_stonith_without_lab_opt_out(repo_root):
    playbook = (repo_root / "ansible/site.yml").read_text(encoding="utf-8")
    group_vars = (repo_root / "ansible/group_vars/all.yml").read_text(encoding="utf-8")

    assert "pacemaker_enable_stonith | default(false) | bool" in playbook
    assert (
        "pacemaker_allow_stonith_disabled_for_lab | default(false) | bool" in playbook
    )
    assert "pacemaker_allow_stonith_disabled_for_lab: false" in group_vars


def test_csi_controller_manifests_drop_privileges(repo_root):
    controller_manifests = [
        repo_root / "csi-arca-storage/deploy/controller.yaml",
        repo_root / "csi-arca-storage/deploy/controller-statefulset.yaml",
        repo_root
        / "csi-arca-storage/deploy/kustomize/base/controller-statefulset.yaml",
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


def test_csi_controller_manifests_wire_liveness_probe(repo_root):
    controller_manifests = [
        repo_root / "csi-arca-storage/deploy/controller.yaml",
        repo_root / "csi-arca-storage/deploy/controller-statefulset.yaml",
        repo_root
        / "csi-arca-storage/deploy/kustomize/base/controller-statefulset.yaml",
    ]

    for manifest_path in controller_manifests:
        manifest = manifest_path.read_text(encoding="utf-8")
        assert "livenessProbe:" in manifest
        assert "path: /healthz" in manifest
        assert "port: 9808" in manifest
        assert "initialDelaySeconds: 10" in manifest
        assert "timeoutSeconds: 3" in manifest
        assert "periodSeconds: 10" in manifest
        assert "failureThreshold: 5" in manifest
        assert "- --health-port=9808" in manifest


def test_csi_deployment_configs_include_node_paths(repo_root):
    config_sources = [
        repo_root / "csi-arca-storage/deploy/controller.yaml",
        repo_root / "csi-arca-storage/deploy/kustomize/base/config.yaml",
        repo_root
        / "csi-arca-storage/deploy/kustomize/overlays/development/config.yaml",
        repo_root / "csi-arca-storage/deploy/kustomize/overlays/production/config.yaml",
    ]

    for config_path in config_sources:
        config = config_path.read_text(encoding="utf-8")
        assert (
            'state_file_path: "/var/lib/csi-arca-storage/node-volumes.json"' in config
        )
        assert (
            'base_mount_path: "/var/lib/kubelet/plugins/csi.arca-storage.io/mounts"'
            in config
        )


def test_development_kustomize_overlay_requires_external_secret_env(repo_root):
    overlay = (
        repo_root
        / "csi-arca-storage/deploy/kustomize/overlays/development/kustomization.yaml"
    ).read_text(encoding="utf-8")
    gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")

    assert "envs:" in overlay
    assert "- secrets.env" in overlay
    assert "literals:" not in overlay
    assert "auth-token=dev-token" not in overlay
    assert (
        repo_root
        / "csi-arca-storage/deploy/kustomize/overlays/development/secrets.env.example"
    ).exists()
    assert (
        "csi-arca-storage/deploy/kustomize/overlays/development/secrets.env"
        in gitignore
    )


def test_python_tool_caches_are_gitignored(repo_root):
    gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")

    for ignored_path in (
        ".mypy_cache/",
        ".ruff_cache/",
        "arca_storage/.mypy_cache/",
        "arca_storage/.ruff_cache/",
    ):
        assert ignored_path in gitignore


def test_csi_node_sidecars_drop_privileges(repo_root):
    node_manifests = [
        repo_root / "csi-arca-storage/deploy/node.yaml",
        repo_root / "csi-arca-storage/deploy/kustomize/base/node.yaml",
    ]

    for manifest_path in node_manifests:
        manifest = manifest_path.read_text(encoding="utf-8")
        assert manifest.count("privileged: true") == 1
        assert manifest.count("allowPrivilegeEscalation: true") == 1
        assert manifest.count("allowPrivilegeEscalation: false") == 2
        assert manifest.count("readOnlyRootFilesystem: true") == 2
        assert manifest.count("- ALL") == 2
        assert "automountServiceAccountToken: false" in manifest
        assert "kubernetes.io/os: linux" in manifest
        assert "seccompProfile:" in manifest
        assert "type: RuntimeDefault" in manifest
        assert "arca.storage.io/pod-security-exception" in manifest
        assert "name: plugin-dir" not in manifest
        assert manifest.count("hostPath:") == 5
        assert manifest.count("mountPropagation: Bidirectional") == 2


def test_csi_node_rbac_does_not_grant_cluster_permissions(repo_root):
    rbac_manifests = [
        repo_root / "csi-arca-storage/deploy/rbac-node.yaml",
        repo_root / "csi-arca-storage/deploy/kustomize/base/rbac-node.yaml",
    ]

    for manifest_path in rbac_manifests:
        manifest = manifest_path.read_text(encoding="utf-8")
        assert "kind: ServiceAccount" in manifest
        assert "automountServiceAccountToken: false" in manifest
        assert "kind: ClusterRole" not in manifest
        assert "kind: ClusterRoleBinding" not in manifest
        assert "resources:" not in manifest
        assert "verbs:" not in manifest


def test_csi_controller_exposes_pod_namespace_to_driver(repo_root):
    controller_manifests = [
        repo_root / "csi-arca-storage/deploy/controller.yaml",
        repo_root / "csi-arca-storage/deploy/controller-statefulset.yaml",
        repo_root / "csi-arca-storage/deploy/kustomize/base/controller-statefulset.yaml",
    ]

    for manifest_path in controller_manifests:
        manifest = manifest_path.read_text(encoding="utf-8")
        assert "- name: POD_NAMESPACE" in manifest
        assert "fieldPath: metadata.namespace" in manifest


def test_csi_workflow_verifies_kubeconform_checksum(repo_root):
    workflow = (repo_root / ".github/workflows/csi-tests.yml").read_text(encoding="utf-8")

    assert "KUBECONFORM_SHA256=" in workflow
    assert "95f14e87aa28c09d5941f11bd024c1d02fdc0303ccaa23f61cef67bc92619d73" in workflow
    assert "sha256sum -c -" in workflow


def test_csi_controller_rbac_limits_crd_discovery(repo_root):
    rbac_manifests = [
        repo_root / "csi-arca-storage/deploy/rbac-controller.yaml",
        repo_root / "csi-arca-storage/deploy/kustomize/base/rbac-controller.yaml",
    ]

    for manifest_path in rbac_manifests:
        manifest = manifest_path.read_text(encoding="utf-8")
        lines = manifest.splitlines()
        crd_line_index = next(
            index
            for index, line in enumerate(lines)
            if line.strip() == 'resources: ["customresourcedefinitions"]'
        )
        next_rule_index = next(
            (
                index
                for index in range(crd_line_index + 1, len(lines))
                if lines[index].startswith("  - ")
            ),
            len(lines),
        )
        crd_rule = "\n".join(lines[crd_line_index - 1 : next_rule_index])

        assert "resourceNames:" in crd_rule
        assert "arcavolumes.storage.arca.io" in crd_rule
        assert "arcasnapshots.storage.arca.io" in crd_rule
        assert 'verbs: ["get"]' in crd_rule
        assert 'verbs: ["get", "list"]' not in crd_rule


def test_csi_example_workloads_do_not_use_latest_image_tag(repo_root):
    example_sources = [
        repo_root / "csi-arca-storage/deploy/examples/pod.yaml",
        repo_root / "csi-arca-storage/docs/quickstart.md",
        repo_root / "csi-arca-storage/docs/quickstart.ja.md",
    ]

    for source_path in example_sources:
        content = source_path.read_text(encoding="utf-8")
        assert ":latest" not in content
        assert "nginx:1.27.5-alpine" in content
