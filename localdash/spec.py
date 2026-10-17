"""Dashboard spec: dataclasses, JSON schema, validation.

The model only ever writes this JSON. Everything visual is decided by the
deterministic renderer, so a spec is small, diffable and easy to hand-edit.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PANEL_TYPES = ("kpi", "line", "area", "bar", "stacked_bar", "pie", "scatter", "table")
XY_TYPES = ("line", "area", "bar", "stacked_bar", "scatter")
FILTER_TYPES = ("select", "date_range")
FORMATS = ("number", "currency", "percent", "compact")
ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
DEFAULT_REFRESH = 60

SPEC_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "localdash dashboard spec",
    "type": "object",
    "required": ["title", "panels"],
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "source": {"type": "string", "description": "File path, directory, or database URL."},
        "refresh_seconds": {"type": "integer", "minimum": 0, "description": "0 disables auto-refresh."},
        "model": {"type": "string", "description": "Model that generated the spec (shown in the header)."},
        "filters": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["column", "type"],
                "additionalProperties": False,
                "properties": {
                    "column": {"type": "string"},
                    "type": {"enum": list(FILTER_TYPES)},
                    "label": {"type": "string"},
                },
            },
        },
        "panels": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "title", "type", "sql"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "pattern": ID_RE.pattern},
                    "title": {"type": "string"},
                    "type": {"enum": list(PANEL_TYPES)},
                    "sql": {"type": "string", "description": "DuckDB SQL."},
                    "x": {"type": "string", "description": "Column for x axis / pie labels."},
                    "y": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Value column(s). A single string is accepted.",
                    },
                    "series": {"type": "string", "description": "Column whose values split the data into series."},
                    "value": {"type": "string", "description": "kpi: column holding the value."},
                    "compare": {"type": "string", "description": "kpi: column holding the previous-period value."},
                    "format": {"enum": list(FORMATS)},
                    "lower_is_better": {"type": "boolean", "description": "kpi: a drop is good (churn, cost, latency)."},
                    "limit": {"type": "integer", "minimum": 1, "description": "table: max rows shown."},
                    "description": {"type": "string"},
                    "span": {"enum": [1, 2], "description": "Grid width: 1 = half, 2 = full row."},
                },
            },
        },
    },
}


class SpecError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class Filter:
    column: str
    type: str
    label: str | None = None


@dataclass
class Panel:
    id: str
    title: str
    type: str
    sql: str
    x: str | None = None
    y: list[str] = field(default_factory=list)
    series: str | None = None
    value: str | None = None
    compare: str | None = None
    format: str | None = None
    lower_is_better: bool | None = None
    limit: int | None = None
    description: str | None = None
    span: int | None = None

    def referenced_columns(self) -> list[str]:
        cols = [self.x, self.series, self.value, self.compare, *self.y]
        return [c for c in cols if c]

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass
class Spec:
    title: str
    panels: list[Panel]
    source: str | None = None
    refresh_seconds: int = DEFAULT_REFRESH
    filters: list[Filter] = field(default_factory=list)
    description: str | None = None
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"title": self.title}
        if self.description:
            d["description"] = self.description
        if self.source is not None:
            d["source"] = self.source
        d["refresh_seconds"] = self.refresh_seconds
        if self.model:
            d["model"] = self.model
        d["filters"] = [_compact(asdict(f)) for f in self.filters]
        d["panels"] = [p.to_dict() for p in self.panels]
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json())

    def panel(self, panel_id: str) -> Panel | None:
        return next((p for p in self.panels if p.id == panel_id), None)


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, [], "")}


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def panel_errors(p: dict[str, Any], where: str = "panel") -> list[str]:
    """Structural errors for a single panel dict."""
    errs: list[str] = []
    if not isinstance(p, dict):
        return [f"{where}: must be an object"]
    pid = p.get("id")
    where = f"panel {pid!r}" if pid else where
    allowed = set(SPEC_JSON_SCHEMA["properties"]["panels"]["items"]["properties"])
    for key in p:
        if key not in allowed:
            errs.append(f"{where}: unknown field {key!r}")
    for key in ("id", "title", "type", "sql"):
        if not isinstance(p.get(key), str) or not p.get(key).strip():
            errs.append(f"{where}: missing or empty {key!r}")
    if isinstance(pid, str) and pid and not ID_RE.match(pid):
        errs.append(f"{where}: id must match {ID_RE.pattern}")
    ptype = p.get("type")
    if isinstance(ptype, str) and ptype not in PANEL_TYPES:
        errs.append(f"{where}: type must be one of {', '.join(PANEL_TYPES)}")
    y = p.get("y")
    if y is not None and not (isinstance(y, str) or (isinstance(y, list) and all(isinstance(i, str) for i in y))):
        errs.append(f"{where}: y must be a string or list of strings")
    for key in ("x", "series", "value", "compare", "title", "description"):
        if p.get(key) is not None and not isinstance(p.get(key), str):
            errs.append(f"{where}: {key!r} must be a string")
    if p.get("format") is not None and p.get("format") not in FORMATS:
        errs.append(f"{where}: format must be one of {', '.join(FORMATS)}")
    if p.get("limit") is not None and (not isinstance(p["limit"], int) or isinstance(p["limit"], bool) or p["limit"] < 1):
        errs.append(f"{where}: limit must be a positive integer")
    if p.get("lower_is_better") is not None and not isinstance(p["lower_is_better"], bool):
        errs.append(f"{where}: lower_is_better must be true or false")
    if p.get("span") is not None and p.get("span") not in (1, 2):
        errs.append(f"{where}: span must be 1 or 2")
    ys = _as_list(y) if not isinstance(y, (int, float, dict)) else []
    if ptype == "kpi" and not p.get("value"):
        errs.append(f"{where}: kpi panels need 'value'")
    if ptype in XY_TYPES:
        if not p.get("x"):
            errs.append(f"{where}: {ptype} panels need 'x'")
        if not ys:
            errs.append(f"{where}: {ptype} panels need 'y'")
    if ptype == "pie":
        if not p.get("x"):
            errs.append(f"{where}: pie panels need 'x' (label column)")
        if len(ys) != 1:
            errs.append(f"{where}: pie panels need exactly one 'y' column")
    return errs


def spec_errors(d: Any) -> list[str]:
    if not isinstance(d, dict):
        return ["spec must be a JSON object"]
    errs: list[str] = []
    for key in d:
        if key not in SPEC_JSON_SCHEMA["properties"]:
            errs.append(f"unknown top-level field {key!r}")
    if not isinstance(d.get("title"), str) or not d.get("title", "").strip():
        errs.append("missing or empty 'title'")
    rs = d.get("refresh_seconds", DEFAULT_REFRESH)
    if not isinstance(rs, int) or isinstance(rs, bool) or rs < 0:
        errs.append("'refresh_seconds' must be a non-negative integer")
    if d.get("source") is not None and not isinstance(d["source"], str):
        errs.append("'source' must be a string")
    filters = d.get("filters", [])
    if not isinstance(filters, list):
        errs.append("'filters' must be a list")
        filters = []
    for i, f in enumerate(filters):
        if not isinstance(f, dict) or not isinstance(f.get("column"), str) or not f.get("column"):
            errs.append(f"filter #{i + 1}: needs a 'column'")
        elif f.get("type") not in FILTER_TYPES:
            errs.append(f"filter {f['column']!r}: type must be one of {', '.join(FILTER_TYPES)}")
    panels = d.get("panels")
    if not isinstance(panels, list) or not panels:
        errs.append("'panels' must be a non-empty list")
        return errs
    seen: set[str] = set()
    for i, p in enumerate(panels):
        errs.extend(panel_errors(p, where=f"panel #{i + 1}"))
        pid = p.get("id") if isinstance(p, dict) else None
        if isinstance(pid, str):
            if pid in seen:
                errs.append(f"duplicate panel id {pid!r}")
            seen.add(pid)
    return errs


def panel_from_dict(p: dict[str, Any]) -> Panel:
    errs = panel_errors(p)
    if errs:
        raise SpecError(errs)
    return Panel(
        id=p["id"],
        title=p["title"],
        type=p["type"],
        sql=p["sql"].strip(),
        x=p.get("x"),
        y=_as_list(p.get("y")),
        series=p.get("series"),
        value=p.get("value"),
        compare=p.get("compare"),
        format=p.get("format"),
        lower_is_better=p.get("lower_is_better"),
        limit=p.get("limit"),
        description=p.get("description"),
        span=p.get("span"),
    )


def spec_from_dict(d: Any) -> Spec:
    errs = spec_errors(d)
    if errs:
        raise SpecError(errs)
    return Spec(
        title=d["title"],
        description=d.get("description"),
        source=d.get("source"),
        refresh_seconds=d.get("refresh_seconds", DEFAULT_REFRESH),
        model=d.get("model"),
        filters=[Filter(column=f["column"], type=f["type"], label=f.get("label")) for f in d.get("filters", [])],
        panels=[panel_from_dict(p) for p in d["panels"]],
    )


def load_spec(path: str | Path) -> Spec:
    text = Path(path).read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SpecError([f"{path}: invalid JSON: {e}"]) from e
    return spec_from_dict(data)


def result_errors(panel: Panel, columns: list[str], row_count: int | None = None) -> list[str]:
    """Check that a panel's query result has the columns the panel references."""
    errs = []
    missing = [c for c in panel.referenced_columns() if c not in columns]
    if missing:
        errs.append(
            f"result columns {columns} do not include {missing}; "
            "make the SELECT aliases match the panel's x/y/series/value/compare fields"
        )
    if panel.type == "kpi" and row_count is not None and row_count != 1:
        errs.append(f"kpi query must return exactly 1 row, got {row_count}")
    if panel.type == "pie" and row_count is not None and row_count > 12:
        errs.append(f"pie query returned {row_count} slices; keep it to 12 or fewer (group the rest as 'Other')")
    return errs
