"""Unit tests for Cinder driver configuration."""

from arca_storage.openstack.cinder import configuration


def test_api_auth_defaults_to_token():
    opts = configuration.get_arca_storage_opts()
    auth_opt = next(opt for opt in opts if opt.name == "arca_storage_api_auth_type")
    assert auth_opt.default == "token"
