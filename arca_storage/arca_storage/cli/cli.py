#!/usr/bin/env python3
"""
Main CLI entry point using Typer.
"""

import sys
from typing import Optional

import typer

from arca_storage.cli.commands import export, svm, volume

app = typer.Typer(
    name="arca",
    help="Arca Storage SVM Control Tool",
    add_completion=False,
)

# Add command groups
app.add_typer(svm.app, name="svm", help="SVM management commands")
app.add_typer(volume.app, name="volume", help="Volume management commands")
app.add_typer(export.app, name="export", help="Export management commands")


def main() -> int:
    """Main entry point."""
    try:
        app()
        return 0
    except KeyboardInterrupt:
        typer.echo("\nOperation cancelled by user", err=True)
        return 130
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
