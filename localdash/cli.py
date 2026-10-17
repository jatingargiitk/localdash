"""localdash command-line interface."""

from __future__ import annotations

import json
import sys

import typer

from . import __version__
from .schema import introspect
from .sources import SourceError, connect

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Turn any CSV or database into a live dashboard. Local, free, any model. Every chart shows its SQL.",
)


def _fail(msg: str) -> None:
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def _main(version: bool = typer.Option(False, "--version", help="Show version and exit.")) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command()
def schema(
    source: str = typer.Argument(..., help="CSV/Parquet/JSON file, directory, SQLite/DuckDB file, or postgres:// URL."),
    no_samples: bool = typer.Option(False, "--no-samples", help="Omit sample rows and top values."),
) -> None:
    """Print a JSON schema summary of SOURCE (tables, columns, types, ranges, sample rows)."""
    try:
        src = connect(source)
    except (SourceError, Exception) as e:  # duckdb errors are plain Exceptions
        _fail(str(e))
    try:
        json.dump(introspect(src, samples=not no_samples), sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    finally:
        src.close()


if __name__ == "__main__":
    app()
