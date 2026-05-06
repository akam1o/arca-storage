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


def test_cinder_client_sets_basic_auth():
    client = ArcaStorageClient(
        api_endpoint="http://127.0.0.1:8080",
        auth_type="basic",
        username="admin",
        password="secret",
    )

    try:
        assert client.session.auth == ("admin", "secret")
    finally:
        client.close()


def test_cinder_client_rejects_missing_token():
    with pytest.raises(ValueError, match="api_token is required"):
        ArcaStorageClient(api_endpoint="http://127.0.0.1:8080", auth_type="token")
