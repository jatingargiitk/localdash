"""Spec + query results -> one self-contained HTML file.

Rendering is deterministic: the same spec and data always produce the same
page. The model never touches HTML.
"""

from __future__ import annotations

import datetime as dt
import time
from importlib import resources
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape

from . import __version__
from .query import DEFAULT_ROW_CAP, Runner
from .sources import redact, is_postgres
from .spec import Panel, Spec, result_errors

CHARTJS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"
DEFAULT_TABLE_LIMIT = 50
THEMES = ("dark", "light")

_env = Environment(
    loader=PackageLoader("localdash", "templates"),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def panel_row_limit(panel: Panel) -> int:
    if panel.type == "table":
        return panel.limit or DEFAULT_TABLE_LIMIT
    return DEFAULT_ROW_CAP


def run_panel(
    runner: Runner,
    panel: Panel,
    filters: list[dict] | None = None,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one panel's SQL. Never raises: errors are returned in the payload."""
    t0 = time.perf_counter()
    try:
        res = runner.run(panel.sql, limit=panel_row_limit(panel), filters=filters, values=values)
    except Exception as e:  # duckdb raises many exception types
        return {"error": _clean_error(e), "ms": round((time.perf_counter() - t0) * 1000, 1)}
    res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    problems = result_errors(panel, res["columns"])
    if problems:
        res["error"] = "; ".join(problems)
    return res


def run_all(runner: Runner, spec: Spec, values: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    filters = [{"column": f.column, "type": f.type} for f in spec.filters]
    return {p.id: run_panel(runner, p, filters, values) for p in spec.panels}


def _clean_error(e: Exception) -> str:
    msg = str(e).strip()
    return msg.split("\n\nLINE")[0].strip() if msg else type(e).__name__


def layout(spec: Spec) -> tuple[list[Panel], list[Panel], dict[str, int]]:
    """KPIs go on the top row; the rest flows into a 2-column grid in spec order.

    Tables default to full width. A half-width card that would sit alone on its
    row (before a full-width card, or at the end) is stretched to full width so
    the grid never has holes. Returns (kpis, rest, spans).
    """
    kpis = [p for p in spec.panels if p.type == "kpi"]
    rest = [p for p in spec.panels if p.type != "kpi"]
    spans = {p.id: p.span or (2 if p.type == "table" else 1) for p in rest}
    col, last_half = 0, None
    for p in rest:
        if spans[p.id] == 2:
            if col == 1 and last_half and not last_half.span:
                spans[last_half.id] = 2
            col = 0
        else:
            col = (col + 1) % 2
            last_half = p if col == 1 else None
    if col == 1 and last_half and not last_half.span:
        spans[last_half.id] = 2
    return kpis, rest, spans


def default_source_label(source: str | None) -> str:
    if not source:
        return ""
    if is_postgres(source):
        return redact(source)
    return Path(source).name or source


def chartjs_source() -> str:
    js = resources.files("localdash").joinpath("vendor/chart.umd.min.js").read_text(encoding="utf-8")
    return js.replace("</script", "<\\/script")


def render_html(
    spec: Spec,
    results: dict[str, dict[str, Any]],
    *,
    theme: str = "dark",
    offline: bool = False,
    mode: str = "static",
    source_label: str | None = None,
    generated_at: dt.datetime | None = None,
    filter_options: dict[str, Any] | None = None,
    spec_version: str | None = None,
) -> str:
    if theme not in THEMES:
        raise ValueError(f"theme must be one of {THEMES}")
    generated_at = generated_at or dt.datetime.now().astimezone()
    kpis, rest, spans = layout(spec)
    panels = []
    for p in kpis + rest:
        d = p.to_dict()
        d["result"] = results.get(p.id, {"error": "not executed"})
        panels.append(d)
    payload = {
        "title": spec.title,
        "description": spec.description,
        "source": source_label or default_source_label(spec.source),
        "model": spec.model or "",
        "refresh_seconds": spec.refresh_seconds,
        "mode": mode,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "filters": [
            {"column": f.column, "type": f.type, "label": f.label or f.column.replace("_", " ").title()}
            for f in spec.filters
        ],
        "filter_options": filter_options or {},
        "spec_version": spec_version,
        "panels": panels,
        "version": __version__,
    }
    template = _env.get_template("dashboard.html.j2")
    return template.render(
        spec=spec,
        kpis=kpis,
        rest=rest,
        spans=spans,
        payload=payload,
        theme=theme,
        mode=mode,
        offline=offline,
        chartjs_inline=chartjs_source() if offline else None,
        chartjs_cdn=CHARTJS_CDN,
        version=__version__,
    )


def render_to_file(
    spec: Spec,
    runner: Runner,
    out: str | Path,
    *,
    theme: str = "dark",
    offline: bool = False,
    source_label: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Run every panel, render static HTML to ``out``, return the results."""
    results = run_all(runner, spec)
    html = render_html(spec, results, theme=theme, offline=offline, source_label=source_label)
    Path(out).write_text(html, encoding="utf-8")
    return results
