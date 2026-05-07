"""Tests for the standalone Cinder ARCA API client."""

import pytest

from arca_storage.openstack.cinder.client import ArcaStorageClient


def test_cinder_client_sets_bearer_token_header():
    client = ArcaStorageClient(
        api_endpoint="http://127.0.0.1:8080",
        auth_type="token",
        api_token="secret-token",
    )

    try:
        assert client.session.headers["Authorization"] == "Bearer secret-token"
    finally:
        client.close()


def test_cinder_client_ca_bundle_overrides_verify_ssl():
    client = ArcaStorageClient(
        api_endpoint="http://127.0.0.1:8080",
        verify_ssl=False,
        ca_bundle="/etc/ssl/certs/arca-ca.pem",
    )

    try:
        assert client.verify_ssl == "/etc/ssl/certs/arca-ca.pem"
    finally:
        client.close()


def test_cinder_client_rejects_missing_token():
    with pytest.raises(ValueError, match="api_token is required"):
        ArcaStorageClient(api_endpoint="http://127.0.0.1:8080", auth_type="token")


def test_cinder_client_rejects_basic_auth():
    with pytest.raises(ValueError, match="Must be 'token' or 'none'"):
        ArcaStorageClient(api_endpoint="http://127.0.0.1:8080", auth_type="basic")
