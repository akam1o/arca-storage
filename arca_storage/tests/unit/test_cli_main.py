"""Tests for the CLI entrypoint error handling."""

from typer.testing import CliRunner

from arca_storage.cli import cli


def test_cli_main_hides_traceback_by_default(monkeypatch, capsys):
    def fail_app(*, args=None):
        raise RuntimeError("boom")

    monkeypatch.delenv("ARCA_CLI_DEBUG", raising=False)
    monkeypatch.setattr(cli, "app", fail_app)

    assert cli.main([]) == 1

    stderr = capsys.readouterr().err
    assert "Error: boom" in stderr
    assert "Traceback (most recent call last)" not in stderr


def test_cli_main_verbose_prints_traceback(monkeypatch, capsys):
    def fail_app(*, args=None):
        raise RuntimeError("boom")

    monkeypatch.delenv("ARCA_CLI_DEBUG", raising=False)
    monkeypatch.setattr(cli, "app", fail_app)

    assert cli.main(["--verbose"]) == 1

    stderr = capsys.readouterr().err
    assert "Error: boom" in stderr
    assert "Traceback (most recent call last)" in stderr
    assert "RuntimeError: boom" in stderr


def test_cli_main_debug_env_prints_traceback(monkeypatch, capsys):
    def fail_app(*, args=None):
        raise RuntimeError("boom")

    monkeypatch.setenv("ARCA_CLI_DEBUG", "true")
    monkeypatch.setattr(cli, "app", fail_app)

    assert cli.main([]) == 1

    stderr = capsys.readouterr().err
    assert "Traceback (most recent call last)" in stderr
    assert "RuntimeError: boom" in stderr


def test_verbose_option_is_documented_in_help():
    result = CliRunner().invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "--verbose" in result.output
