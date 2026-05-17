"""Tests for the API server entrypoint."""

from types import SimpleNamespace

import pytest

from arca_storage.api import server


def test_help_does_not_require_config(monkeypatch, capsys):
    def fail_load_settings():
        raise AssertionError("load_settings should not be called for --help")

    monkeypatch.setattr(server, "load_settings", fail_load_settings)

    with pytest.raises(SystemExit) as exc:
        server.main(["--help"])

    assert exc.value.code == 0
    assert "arca-storage-api" in capsys.readouterr().out


def _settings(bind: str = "127.0.0.1", port: int = 8080):
    return SimpleNamespace(api=SimpleNamespace(bind=bind, port=port))


def test_loopback_bind_requires_token_without_opt_out(monkeypatch):
    def fail_run(*args, **kwargs):
        raise AssertionError("uvicorn.run should not be called without an API token")

    monkeypatch.delenv("ARCA_API_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_ALLOW_UNAUTHENTICATED_LOOPBACK", raising=False)
    monkeypatch.setattr(server, "load_settings", lambda: _settings())
    monkeypatch.setattr(server.uvicorn, "run", fail_run)

    with pytest.raises(SystemExit) as exc:
        server.main([])

    assert exc.value.code == 2


def test_loopback_bind_allows_explicit_unauthenticated_opt_out(monkeypatch):
    calls = []
    monkeypatch.delenv("ARCA_API_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ARCA_ALLOW_UNAUTHENTICATED_LOOPBACK", "true")
    monkeypatch.setattr(server, "load_settings", lambda: _settings())
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert server.main([]) == 0

    assert calls[0][1]["host"] == "127.0.0.1"


def test_non_loopback_bind_requires_token(monkeypatch, capsys):
    def fail_run(*args, **kwargs):
        raise AssertionError("uvicorn.run should not be called without an API token")

    monkeypatch.delenv("ARCA_API_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_ALLOW_UNAUTHENTICATED_LOOPBACK", raising=False)
    monkeypatch.setattr(server, "load_settings", lambda: _settings(bind="0.0.0.0"))
    monkeypatch.setattr(server.uvicorn, "run", fail_run)

    with pytest.raises(SystemExit) as exc:
        server.main([])

    assert exc.value.code == 2
    assert "ARCA_API_TOKEN or ARCA_AUTH_TOKEN is required" in capsys.readouterr().err


def test_host_override_requires_token(monkeypatch):
    monkeypatch.delenv("ARCA_API_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ARCA_ALLOW_UNAUTHENTICATED_LOOPBACK", raising=False)
    monkeypatch.setattr(server, "load_settings", lambda: _settings())

    with pytest.raises(SystemExit) as exc:
        server.main(["--host", "192.0.2.10"])

    assert exc.value.code == 2


def test_non_loopback_bind_with_token_requires_tls_by_default(monkeypatch, capsys):
    def fail_run(*args, **kwargs):
        raise AssertionError("uvicorn.run should not be called without TLS")

    monkeypatch.setenv("ARCA_API_TOKEN", "secret-token")
    monkeypatch.delenv("ARCA_ALLOW_INSECURE_REMOTE_API", raising=False)
    monkeypatch.setattr(server, "load_settings", lambda: _settings(bind="0.0.0.0"))
    monkeypatch.setattr(server.uvicorn, "run", fail_run)

    with pytest.raises(SystemExit) as exc:
        server.main([])

    assert exc.value.code == 2
    assert "TLS is required" in capsys.readouterr().err


def test_non_loopback_bind_accepts_token_with_tls(monkeypatch):
    calls = []
    monkeypatch.setenv("ARCA_API_TOKEN", "secret-token")
    monkeypatch.delenv("ARCA_ALLOW_INSECURE_REMOTE_API", raising=False)
    monkeypatch.setattr(server, "load_settings", lambda: _settings(bind="0.0.0.0"))
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert server.main(["--ssl-certfile", "/etc/arca-storage/api.crt", "--ssl-keyfile", "/etc/arca-storage/api.key"]) == 0

    assert calls[0][1]["host"] == "0.0.0.0"
    assert calls[0][1]["ssl_certfile"] == "/etc/arca-storage/api.crt"
    assert calls[0][1]["ssl_keyfile"] == "/etc/arca-storage/api.key"


def test_non_loopback_bind_accepts_explicit_insecure_remote_override(monkeypatch):
    calls = []
    monkeypatch.setenv("ARCA_API_TOKEN", "secret-token")
    monkeypatch.setenv("ARCA_ALLOW_INSECURE_REMOTE_API", "true")
    monkeypatch.setattr(server, "load_settings", lambda: _settings(bind="0.0.0.0"))
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert server.main([]) == 0

    assert calls[0][1]["host"] == "0.0.0.0"
    assert calls[0][1]["ssl_certfile"] is None


def test_ssl_keyfile_requires_ssl_certfile(monkeypatch):
    monkeypatch.setenv("ARCA_API_TOKEN", "secret-token")
    monkeypatch.setattr(server, "load_settings", lambda: _settings())

    with pytest.raises(SystemExit) as exc:
        server.main(["--ssl-keyfile", "/etc/arca-storage/api.key"])

    assert exc.value.code == 2
