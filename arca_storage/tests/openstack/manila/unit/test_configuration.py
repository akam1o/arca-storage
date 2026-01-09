"""Unit tests for Manila driver configuration."""

import pytest

from arca_storage.openstack.manila import configuration


def test_get_arca_manila_opts_smoke():
    # oslo_config is required for these options; if missing, this should raise ImportError.
    opts = configuration.get_arca_manila_opts()
    assert isinstance(opts, list)
    assert any(opt.name == "arca_storage_api_endpoint" for opt in opts)
    assert any(opt.name == "arca_storage_svm_strategy" for opt in opts)


def test_svm_strategy_choices():
    opts = configuration.get_arca_manila_opts()
    svm_opt = next(opt for opt in opts if opt.name == "arca_storage_svm_strategy")
    assert set(svm_opt.type.choices.keys()) == {"shared", "per_project", "manual"}


def test_per_project_ip_pools_is_multistr():
    opts = configuration.get_arca_manila_opts()
    pools_opt = next(opt for opt in opts if opt.name == "arca_storage_per_project_ip_pools")
    # oslo.config uses MultiStrOpt class; type name is stable enough for this assertion.
    assert pools_opt.__class__.__name__ == "MultiStrOpt"


@pytest.mark.parametrize(
    "name",
    [
        "arca_storage_snapshot_support",
        "arca_storage_create_share_from_snapshot_support",
        "arca_storage_revert_to_snapshot_support",
        "arca_storage_mount_snapshot_support",
    ],
)
def test_snapshot_feature_flags_exist(name):
    opts = configuration.get_arca_manila_opts()
    assert any(opt.name == name for opt in opts)
