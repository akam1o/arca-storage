"""Unit tests for Cinder driver configuration."""

from arca_storage.openstack.cinder import configuration


def test_api_auth_defaults_to_token():
    opts = configuration.get_arca_storage_opts()
    auth_opt = next(opt for opt in opts if opt.name == "arca_storage_api_auth_type")
    assert auth_opt.default == "token"


def test_insecure_api_token_transport_requires_opt_in():
    opts = configuration.get_arca_storage_opts()
    opt = next(
        opt
        for opt in opts
        if opt.name == "arca_storage_allow_insecure_api_token_transport"
    )
    assert opt.default is False
