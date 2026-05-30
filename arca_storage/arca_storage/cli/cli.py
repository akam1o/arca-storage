#!/usr/bin/env python3
"""
Main CLI entry point using Typer.
"""

import os
import sys
import traceback
from typing import List, Optional

import typer

from arca_storage.cli.commands import export, svm, volume
from arca_storage.cli.commands import bootstrap

app = typer.Typer(
    name="arca",
    help="Arca Storage SVM Control Tool",
    add_completion=False,
)

# Add command groups
app.add_typer(svm.app, name="svm", help="SVM management commands")
app.add_typer(volume.app, name="volume", help="Volume management commands")
app.add_typer(export.app, name="export", help="Export management commands")
app.add_typer(bootstrap.app, name="bootstrap", help="Bootstrap initial setup")

_VERBOSE_ERRORS = False
_TRUE_VALUES = {"1", "true", "yes", "on"}


@app.callback()
def global_options(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show traceback for unexpected errors."
    ),
) -> None:
    """Configure global CLI options."""
    global _VERBOSE_ERRORS
    _VERBOSE_ERRORS = verbose


def _debug_tracebacks_enabled(argv: Optional[List[str]]) -> bool:
    args = sys.argv[1:] if argv is None else argv
    env_value = os.environ.get("ARCA_CLI_DEBUG", "")
    return (
        _VERBOSE_ERRORS
        or "--verbose" in args
        or "-v" in args
        or env_value.strip().lower() in _TRUE_VALUES
    )


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point."""
    global _VERBOSE_ERRORS
    _VERBOSE_ERRORS = False
    try:
        if argv is None:
            app()
        else:
            app(args=argv)
        return 0
    except KeyboardInterrupt:
        typer.echo("\nOperation cancelled by user", err=True)
        return 130
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        if _debug_tracebacks_enabled(argv):
            traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
