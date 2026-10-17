"""localdash command-line interface."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .llm import LLMConfig, LLMError, complete
from .planner import PlanError, plan
from .planner import edit as edit_spec
from .query import Runner
from .render import render_to_file, run_panel
from .schema import introspect
from .sources import SourceError, connect, is_postgres
from .spec import SpecError, load_spec, spec_errors

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


def _open_spec(spec_path: str, source_override: str | None = None):
    try:
        spec = load_spec(spec_path)
    except FileNotFoundError:
        _fail(f"spec not found: {spec_path}")
    except SpecError as e:
        _fail("invalid spec:\n  - " + "\n  - ".join(e.errors))
    source = source_override or spec.source
    if not source:
        _fail("the spec has no 'source'; pass --source")
    try:
        src = connect(source, base_dir=Path(spec_path).resolve().parent)
    except Exception as e:
        _fail(str(e))
    return spec, src


def _report(spec, results: dict) -> int:
    failed = 0
    for p in spec.panels:
        res = results[p.id]
        if res.get("error"):
            failed += 1
            typer.secho(f"  ✗ {p.id}: {res['error']}", fg=typer.colors.RED, err=True)
        else:
            n = len(res["rows"])
            typer.secho(f"  ✓ {p.id} ({p.type}, {n}{'+' if res['truncated'] else ''} rows, {res['ms']} ms)", err=True)
    return failed


@app.command()
def render(
    spec_path: str = typer.Argument(..., metavar="SPEC", help="Path to a dashboard JSON spec."),
    out: Optional[str] = typer.Option(None, "-o", "--out", help="Output HTML file (default: SPEC with .html)."),
    theme: str = typer.Option("dark", "--theme", help="dark or light."),
    offline: bool = typer.Option(False, "--offline", help="Inline Chart.js so the file works with no internet."),
    source: Optional[str] = typer.Option(None, "--source", help="Override the spec's data source."),
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero if any panel fails."),
) -> None:
    """Render a dashboard from an existing spec. No model call."""
    spec, src = _open_spec(spec_path, source)
    out = out or str(Path(spec_path).with_suffix(".html"))
    try:
        results = render_to_file(spec, Runner(src), out, theme=theme, offline=offline)
    except ValueError as e:
        _fail(str(e))
    finally:
        src.close()
    failed = _report(spec, results)
    typer.secho(f"wrote {out}", fg=typer.colors.GREEN, err=True)
    if failed and strict:
        raise typer.Exit(2)


@app.command()
def validate(
    spec_path: str = typer.Argument(..., metavar="SPEC", help="Path to a dashboard JSON spec."),
    source: Optional[str] = typer.Option(None, "--source", help="Override the spec's data source."),
) -> None:
    """Check a spec: structure, then run every panel's SQL. Prints JSON; exits 2 if anything fails."""
    try:
        data = json.loads(Path(spec_path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        _fail(str(e))
    errs = spec_errors(data)
    if errs:
        json.dump({"ok": False, "spec_errors": errs, "panels": {}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        raise typer.Exit(2)
    spec, src = _open_spec(spec_path, source)
    try:
        runner = Runner(src)
        report = {}
        for p in spec.panels:
            res = run_panel(runner, p)
            report[p.id] = (
                {"ok": False, "error": res["error"], "columns": res.get("columns")}
                if res.get("error")
                else {"ok": True, "columns": res["columns"], "rows": len(res["rows"]), "first_rows": res["rows"][:3]}
            )
        known = {c for cols in runner.table_columns.values() for c in cols}
        filter_errs = [f"filter column {f.column!r} not found in any table" for f in spec.filters if f.column not in known]
    finally:
        src.close()
    ok = all(r["ok"] for r in report.values()) and not filter_errs
    json.dump({"ok": ok, "spec_errors": filter_errs, "panels": report}, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    if not ok:
        raise typer.Exit(2)


def _llm(provider: str | None, model: str | None):
    try:
        config = LLMConfig.from_env(provider, model)
    except LLMError as e:
        _fail(str(e))
    return config, (lambda messages: complete(messages, config))


def _spec_source(source: str, spec_dir: Path) -> str:
    """Store file sources relative to the spec so the folder can be moved or shared."""
    if is_postgres(source) or source.startswith("sqlite:") or not Path(source).exists():
        return source
    return os.path.relpath(Path(source).resolve(), spec_dir.resolve())


def _log(msg: str) -> None:
    typer.secho(msg, err=True, fg=typer.colors.RED if "✗" in msg else None)


@app.command()
def build(
    source: str = typer.Argument(..., help="Data source (file, directory, SQLite/DuckDB file, postgres:// URL)."),
    request: str = typer.Argument(..., help='What you want to see, e.g. "weekly signups and revenue by plan".'),
    out: str = typer.Option("dashboard.html", "-o", "--out", help="Output HTML; the spec is saved next to it as .json."),
    provider: Optional[str] = typer.Option(None, "--provider", help="ollama, openrouter, openai or anthropic (default: $LOCALDASH_PROVIDER)."),
    model: Optional[str] = typer.Option(None, "--model", help="Model name (default: $LOCALDASH_MODEL or provider default)."),
    theme: str = typer.Option("dark", "--theme", help="dark or light."),
    offline: bool = typer.Option(False, "--offline", help="Inline Chart.js so the file works with no internet."),
    no_samples: bool = typer.Option(False, "--no-samples", help="Send only the schema to the model, no sample rows or values."),
    retries: int = typer.Option(3, "--retries", help="Fix attempts per failing panel."),
) -> None:
    """Plan a dashboard with a model, save the spec next to the HTML, render it."""
    config, llm = _llm(provider, model)
    try:
        src = connect(source)
    except Exception as e:
        _fail(str(e))
    out_path = Path(out)
    spec_path = out_path.with_suffix(".json")
    try:
        summary = introspect(src, samples=not no_samples)
        runner = Runner(src)
        typer.secho(f"planning with {config.label} ({len(src.tables)} table{'s' if len(src.tables) != 1 else ''})", err=True, bold=True)
        try:
            spec, warnings = plan(runner, summary, request, llm, max_retries=retries, log=_log,
                                  source=_spec_source(source, out_path.parent), model_label=config.label)
        except (PlanError, LLMError) as e:
            _fail(str(e))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        spec.save(spec_path)
        render_to_file(spec, runner, out_path, theme=theme, offline=offline)
    finally:
        src.close()
    for w in warnings:
        typer.secho(f"warning: {w}", fg=typer.colors.YELLOW, err=True)
    typer.secho(f"wrote {spec_path} and {out_path} ({len(spec.panels)} panels)", fg=typer.colors.GREEN, err=True)


@app.command()
def edit(
    spec_path: str = typer.Argument(..., metavar="SPEC", help="Path to a dashboard JSON spec."),
    change: str = typer.Argument(..., help='e.g. "add a churn panel" or "make signups a bar chart".'),
    out: Optional[str] = typer.Option(None, "-o", "--out", help="Output HTML (default: SPEC with .html)."),
    provider: Optional[str] = typer.Option(None, "--provider", help="ollama, openrouter, openai or anthropic."),
    model: Optional[str] = typer.Option(None, "--model", help="Model name."),
    theme: str = typer.Option("dark", "--theme", help="dark or light."),
    offline: bool = typer.Option(False, "--offline", help="Inline Chart.js so the file works with no internet."),
    no_samples: bool = typer.Option(False, "--no-samples", help="Send only the schema to the model."),
    retries: int = typer.Option(3, "--retries", help="Fix attempts per failing panel."),
) -> None:
    """Change an existing dashboard in plain English, re-validate every panel, re-render."""
    config, llm = _llm(provider, model)
    spec, src = _open_spec(spec_path)
    out = out or str(Path(spec_path).with_suffix(".html"))
    try:
        runner = Runner(src)
        summary = introspect(src, samples=not no_samples)
        typer.secho(f"editing with {config.label}", err=True, bold=True)
        try:
            new_spec, warnings = edit_spec(runner, summary, spec, change, llm, max_retries=retries, log=_log,
                                           model_label=config.label)
        except (PlanError, LLMError) as e:
            _fail(str(e))
        backup = Path(spec_path).with_suffix(".json.bak")
        backup.write_text(Path(spec_path).read_text())
        new_spec.save(spec_path)
        render_to_file(new_spec, runner, out, theme=theme, offline=offline)
    finally:
        src.close()
    old = {p.id: p.to_dict() for p in spec.panels}
    new = {p.id: p.to_dict() for p in new_spec.panels}
    for pid in new:
        if pid not in old:
            typer.secho(f"  + {pid}", fg=typer.colors.GREEN, err=True)
        elif new[pid] != old[pid]:
            typer.secho(f"  ~ {pid}", fg=typer.colors.YELLOW, err=True)
    for pid in old:
        if pid not in new:
            typer.secho(f"  - {pid}", fg=typer.colors.RED, err=True)
    for w in warnings:
        typer.secho(f"warning: {w}", fg=typer.colors.YELLOW, err=True)
    typer.secho(f"wrote {spec_path} (previous: {backup}) and {out}", fg=typer.colors.GREEN, err=True)


@app.command()
def serve(
    spec_path: str = typer.Argument(..., metavar="SPEC", help="Path to a dashboard JSON spec."),
    port: int = typer.Option(8765, "--port", "-p", help="Port (the next free one is used if taken)."),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind. Default is local-only."),
    no_open: bool = typer.Option(False, "--no-open", help="Don't open a browser."),
    theme: str = typer.Option("dark", "--theme", help="dark or light."),
    refresh: Optional[int] = typer.Option(None, "--refresh", help="Override refresh_seconds."),
    source: Optional[str] = typer.Option(None, "--source", help="Override the spec's data source."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log every request."),
) -> None:
    """Serve a live dashboard that re-runs its queries every refresh_seconds."""
    from .server import serve as run_server

    def ready(url, dash):
        typer.secho(f"localdash serving {dash.spec.title!r} at {url}", fg=typer.colors.GREEN, bold=True, err=True)
        typer.secho(f"  {len(dash.spec.panels)} panels · refresh every {dash.spec.refresh_seconds}s · Ctrl+C to stop", err=True)

    try:
        run_server(spec_path, host=host, port=port, open_browser=not no_open, theme=theme,
                   source_override=source, refresh_override=refresh, quiet=not verbose, on_ready=ready)
    except FileNotFoundError:
        _fail(f"spec not found: {spec_path}")
    except SpecError as e:
        _fail("invalid spec:\n  - " + "\n  - ".join(e.errors))
    except Exception as e:
        _fail(str(e))


@app.command()
def query(
    source: str = typer.Argument(..., help="Data source."),
    sql: str = typer.Argument(..., help="DuckDB SQL to run."),
    limit: int = typer.Option(50, "--limit", help="Max rows to print."),
) -> None:
    """Run ad-hoc SQL against SOURCE and print JSON (handy when writing specs by hand)."""
    try:
        src = connect(source)
        res = Runner(src).run(sql, limit=limit)
    except Exception as e:
        _fail(str(e).split("\n\nLINE")[0])
    json.dump(res, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    app()
