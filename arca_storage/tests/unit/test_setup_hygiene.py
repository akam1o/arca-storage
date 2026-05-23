"""Regression tests for legacy setup.py guardrails."""

import ast


def test_legacy_setup_warns_on_version_fallback(repo_root):
    setup_script = (repo_root / "arca_storage/setup.py").read_text(encoding="utf-8")
    tree = ast.parse(setup_script)

    broad_handlers = [
        handler
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler)
        and (
            handler.type is None
            or (
                isinstance(handler.type, ast.Name)
                and handler.type.id in {"Exception", "BaseException"}
            )
        )
    ]

    assert broad_handlers == []
    assert "warnings.warn" in setup_script
    assert "setuptools-scm is unavailable" in setup_script
