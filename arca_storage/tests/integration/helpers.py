"""
Helpers shared by integration tests.
"""


def cli_output(result) -> str:
    """Return Click/Typer CLI output across mixed and split stderr modes."""
    output = result.output
    if getattr(result, "stderr_bytes", None) is not None:
        output += result.stderr
    return output
