"""Schema + plain-English request -> validated dashboard spec.

Loop: ask the model for spec JSON, parse it, then for every panel run its SQL
(with a LIMIT) and check the result has the columns the panel references. A
failing panel gets its error sent back ("fix only this panel") up to
``max_retries`` times; panels that still fail are dropped with a warning.
Nothing reaches the spec without having executed successfully.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .query import Runner
from .schema import schema_for_prompt
from .spec import (
    FILTER_TYPES,
    SPEC_JSON_SCHEMA,
    Spec,
    panel_errors,
    panel_from_dict,
    result_errors,
    spec_from_dict,
)

LLM = Callable[[list[dict[str, str]]], str]
VALIDATION_LIMIT = 1000

SYSTEM_PROMPT = """You are a data analyst. Return ONLY a JSON object (no prose, no markdown fences) describing a dashboard:
{"title": str, "description": str, "refresh_seconds": 60, "filters": [{"column": str, "type": "select"|"date_range"}], "panels": [panel, ...]}
panel = {"id": snake_case, "title": str, "type": one of kpi|line|area|bar|stacked_bar|pie|scatter|table, "sql": str, ...}
- kpi: SQL returns exactly 1 row. "value": column. Optional "compare": previous-period column, "lower_is_better": true for churn/cost.
- line, area, bar, stacked_bar, scatter: "x": column, "y": [column, ...]. Optional "series": a column whose values become separate lines/bars (long format).
- pie: "x": label column, "y": [one value column], max 8 rows.
- table: optional "limit".
- Optional "format": "currency" | "percent" (a 0-1 ratio) | "compact" | "number".

Rules:
- Use DuckDB SQL. Only reference tables and columns from the provided schema; double-quote identifiers with spaces or capitals.
- x/y/series/value/compare must exactly match column aliases in the SELECT.
- Prefer 4-8 panels: 1-4 KPIs, then trends, then breakdowns, then one table.
- Aggregate time with date_trunc('day'|'week'|'month', col) and ORDER BY time.
- Anchor relative periods to the data, not today: e.g. WHERE d > (SELECT max(d) FROM t) - INTERVAL 30 DAY.
- Never SELECT * on large tables. Tables pick columns, ORDER BY and LIMIT.
- Filters: "select" for low-cardinality text columns, "date_range" for the main date column. Only real columns.
- Write SQL across multiple lines for readability.

Example panels (for a table "orders" with columns ordered_at, plan, amount, customer):
{"id": "kpi_revenue", "title": "Revenue (30d)", "type": "kpi", "format": "currency", "value": "revenue", "compare": "prev_revenue",
 "sql": "WITH a AS (SELECT max(ordered_at) AS d FROM orders)\\nSELECT\\n  sum(amount) FILTER (WHERE ordered_at > d - INTERVAL 30 DAY) AS revenue,\\n  sum(amount) FILTER (WHERE ordered_at <= d - INTERVAL 30 DAY AND ordered_at > d - INTERVAL 60 DAY) AS prev_revenue\\nFROM orders, a"}
{"id": "weekly_revenue", "title": "Weekly revenue by plan", "type": "line", "x": "week", "y": ["revenue"], "series": "plan",
 "sql": "SELECT date_trunc('week', ordered_at) AS week, plan, sum(amount) AS revenue\\nFROM orders\\nGROUP BY 1, 2\\nORDER BY 1"}
{"id": "top_customers", "title": "Top customers", "type": "table", "limit": 20,
 "sql": "SELECT customer, count(*) AS orders, sum(amount) AS revenue\\nFROM orders\\nGROUP BY 1\\nORDER BY revenue DESC\\nLIMIT 20"}"""

TYPE_HINTS = {
    "kpi": 'kpi needs "value": the name of a column in the single result row (and optionally "compare").',
    "pie": 'pie needs "x": the label column and "y": [the value column]; at most 8 rows.',
    "table": "table just needs valid SQL with explicit columns, ORDER BY and LIMIT.",
}
XY_HINT = '{type} needs "x": a column for the x axis and "y": [one or more numeric columns]; optional "series".'

USER_TEMPLATE = """Request: {request}

Schema (DuckDB, {n_tables} table{s}):
{schema}

Return the dashboard JSON now."""

EDIT_TEMPLATE = """Current dashboard spec:
{spec}

Schema (DuckDB):
{schema}

Change request: {change}

Return the COMPLETE updated dashboard JSON. Keep panels that the change does not touch exactly as they are, including their ids and SQL."""

RETRY_TEMPLATE = """Panel "{id}" failed validation.
SQL:
{sql}
Error: {error}
{hint}
Fix only this panel. Return the panel JSON object only (with "id": "{id}")."""

BINDING_ERR = re.compile(r"need 'value'|need 'x'|need 'y'|exactly one 'y'")


class PlanError(RuntimeError):
    pass


# ----------------------------------------------------------------- parsing
def extract_json(text: str) -> Any:
    """Parse the first JSON object in a model reply (tolerates fences and chatter)."""
    if text is None:
        raise ValueError("empty reply")
    t = text.strip()
    t = re.sub(r"^<think>.*?</think>\s*", "", t, flags=re.S)
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object found in reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[start : i + 1])
    raise ValueError("unterminated JSON object in reply")


PANEL_FIELDS = set(SPEC_JSON_SCHEMA["properties"]["panels"]["items"]["properties"])
TOP_FIELDS = set(SPEC_JSON_SCHEMA["properties"])


def normalize_panel(p: Any) -> Any:
    """Lenient clean-up of model output: drop unknown keys, coerce obvious shapes."""
    if not isinstance(p, dict):
        return p
    if "panel" in p and isinstance(p["panel"], dict):
        p = p["panel"]
    p = {k: v for k, v in p.items() if k in PANEL_FIELDS and v is not None}
    if isinstance(p.get("type"), str):
        p["type"] = p["type"].strip().lower().replace("-", "_").replace(" ", "_")
        p["type"] = {"stacked": "stacked_bar", "donut": "pie", "doughnut": "pie", "number": "kpi", "metric": "kpi"}.get(p["type"], p["type"])
    if isinstance(p.get("y"), str):
        p["y"] = [p["y"]]
    if p.get("type") == "pie" and isinstance(p.get("y"), list) and len(p["y"]) > 1:
        p["y"] = p["y"][:1]
    if isinstance(p.get("id"), str):
        p["id"] = re.sub(r"[^a-zA-Z0-9_-]+", "_", p["id"]).strip("_")[:64] or "panel"
        if not p["id"][0].isalpha():
            p["id"] = "p_" + p["id"]
    if isinstance(p.get("limit"), str) and p["limit"].isdigit():
        p["limit"] = int(p["limit"])
    if p.get("format") not in (None, "number", "currency", "percent", "compact"):
        p.pop("format")
    if p.get("span") not in (None, 1, 2):
        p.pop("span")
    if isinstance(p.get("sql"), str):
        p["sql"] = pretty_sql(p["sql"].strip().rstrip(";").strip())
    return p


CLAUSES = ("LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "FULL JOIN", "CROSS JOIN", "JOIN", "FROM", "WHERE",
           "GROUP BY", "HAVING", "QUALIFY", "WINDOW", "ORDER BY", "LIMIT", "UNION ALL", "UNION")
_CLAUSE_RE = re.compile(r"\b(" + "|".join(c.replace(" ", r"\s+") for c in CLAUSES) + r")\b", re.I)


def pretty_sql(sql: str) -> str:
    """Break a one-line query before top-level clauses. Whitespace-only change."""
    if "\n" in sql or len(sql) < 70:
        return sql
    out, depth, i, in_str = [], 0, 0, False
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
                m = _CLAUSE_RE.match(sql, i)
                if m and out and out[-1] == " ":
                    out[-1] = "\n"
        out.append(ch)
        i += 1
    return "".join(out)


def _numeric_columns(columns: list[str], rows: list[list[Any]]) -> list[str]:
    out = []
    for i, c in enumerate(columns):
        vals = [r[i] for r in rows if r[i] is not None]
        if vals and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            out.append(c)
    return out


def autobind(p: dict[str, Any], columns: list[str], rows: list[list[Any]], log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    """Fill missing/invalid x, y, series, value bindings from the result shape."""
    p = dict(p)
    ptype = p.get("type")
    numeric = _numeric_columns(columns, rows)
    other = [c for c in columns if c not in numeric]
    changed = []

    def ok(key):
        return isinstance(p.get(key), str) and p[key] in columns

    if ptype == "kpi":
        if not ok("value") and len(rows) == 1 and numeric:
            p["value"] = numeric[0]
            changed.append("value")
        if p.get("compare") and not ok("compare"):
            rest = [c for c in numeric if c != p.get("value")]
            if rest:
                p["compare"] = rest[0]
            else:
                p.pop("compare")
            changed.append("compare")
    elif ptype in ("line", "area", "bar", "stacked_bar", "scatter", "pie"):
        if not ok("x"):
            candidates = other or columns[:1]
            if ptype == "scatter" and len(numeric) >= 2:
                candidates = numeric[:1]
            if candidates:
                p["x"] = candidates[0]
                changed.append("x")
        if p.get("series") and not ok("series"):
            p.pop("series")
            changed.append("series")
        ys = p.get("y") if isinstance(p.get("y"), list) else []
        if not ys or any(y not in columns for y in ys):
            pool = [c for c in numeric if c not in (p.get("x"), p.get("series"))]
            if pool:
                p["y"] = pool[:1] if ptype == "pie" else pool[:4]
                changed.append("y")
        if ptype in ("line", "area", "stacked_bar") and not p.get("series") and len(other) == 2 and p.get("x") in other and len(p.get("y", [])) == 1:
            p["series"] = next(c for c in other if c != p["x"])
            changed.append("series")
    if changed:
        log(f"    bound {', '.join(f'{k}={p.get(k)!r}' for k in changed)} from result columns for {p.get('id')}")
    return p


# --------------------------------------------------------------- planning
class Planner:
    def __init__(
        self,
        runner: Runner,
        summary: dict[str, Any],
        llm: LLM,
        *,
        max_retries: int = 3,
        log: Callable[[str], None] = lambda msg: None,
    ):
        self.runner = runner
        self.summary = summary
        self.llm = llm
        self.max_retries = max_retries
        self.log = log
        self.warnings: list[str] = []
        self.columns = {c["name"] for t in summary["tables"] for c in t["columns"]}

    # ---- model calls
    def _ask_for_spec(self, messages: list[dict[str, str]]) -> tuple[dict[str, Any], str]:
        last_err = ""
        for attempt in range(1 + self.max_retries):
            reply = self.llm(messages)
            try:
                data = extract_json(reply)
                if isinstance(data, dict) and isinstance(data.get("panels"), list) and data["panels"]:
                    return data, reply
                last_err = "the JSON object must have a non-empty 'panels' list"
            except (ValueError, json.JSONDecodeError) as e:
                last_err = f"could not parse JSON: {e}"
            self.log(f"  model reply rejected ({last_err}); retrying")
            messages = messages + [
                {"role": "assistant", "content": reply or ""},
                {"role": "user", "content": f"That reply was not usable: {last_err}. Return ONLY the dashboard JSON object."},
            ]
        raise PlanError(f"model did not return a usable spec: {last_err}")

    # ---- validation
    def check_panel(self, p: Any) -> tuple[Any, str | None]:
        """Run the panel's SQL and validate it. Returns (panel, error or None).

        Missing column bindings (kpi value, x/y) are filled in from the shape
        of the query result when unambiguous, since small models often write
        correct SQL but forget to name the columns.
        """
        if not isinstance(p, dict):
            return p, "panel must be a JSON object"
        pre = [e for e in panel_errors(p) if not BINDING_ERR.search(e)]
        if pre:
            return p, "; ".join(pre)
        try:
            res = self.runner.run(p["sql"], limit=VALIDATION_LIMIT)
        except Exception as e:
            return p, str(e).split("\n\nLINE")[0].strip() or type(e).__name__
        if not res["rows"]:
            return p, "query returned 0 rows; check the WHERE clause and the date window against the data ranges in the schema"
        p = autobind(p, res["columns"], res["rows"], log=self.log)
        errs = panel_errors(p)
        if errs:
            return p, "; ".join(errs)
        problems = result_errors(panel_from_dict(p), res["columns"], len(res["rows"]))
        return p, ("; ".join(problems) if problems else None)

    def _repair(self, base: list[dict[str, str]], p: dict[str, Any], error: str) -> dict[str, Any] | None:
        pid = p.get("id", "panel")
        messages = list(base)
        for attempt in range(1, self.max_retries + 1):
            self.log(f"  ✗ {pid}: {error}  (retry {attempt}/{self.max_retries})")
            ptype = p.get("type", "")
            hint = TYPE_HINTS.get(ptype) or (XY_HINT.format(type=ptype) if ptype else "")
            messages.append({"role": "user", "content": RETRY_TEMPLATE.format(id=pid, sql=p.get("sql", ""), error=error, hint=hint)})
            reply = self.llm(messages)
            messages.append({"role": "assistant", "content": reply or ""})
            try:
                fixed = extract_json(reply)
            except (ValueError, json.JSONDecodeError) as e:
                error = f"reply was not a JSON object: {e}"
                continue
            if isinstance(fixed, dict) and isinstance(fixed.get("panels"), list):
                fixed = next((q for q in fixed["panels"] if isinstance(q, dict) and q.get("id") == pid), fixed["panels"][0] if fixed["panels"] else {})
            fixed = normalize_panel(fixed)
            if isinstance(fixed, dict):
                fixed.setdefault("id", pid)
                for key in ("title", "type"):
                    if key not in fixed and key in p:
                        fixed[key] = p[key]
                p = fixed
            p, err = self.check_panel(p)
            if err is None:
                self.log(f"  ✓ {pid} (fixed)")
                return p
            error = err
        return None

    def finalize(self, data: dict[str, Any], base: list[dict[str, str]], keep: dict[str, Any] | None = None) -> Spec:
        """Validate every panel (repairing or dropping), clean filters, return a Spec."""
        keep = keep or {}
        panels: list[dict[str, Any]] = []
        seen: set[str] = set()
        for i, raw in enumerate(data.get("panels", [])):
            p = normalize_panel(raw)
            if not isinstance(p, dict):
                self.warnings.append(f"panel #{i + 1} is not an object; dropped")
                continue
            p.setdefault("id", f"panel_{i + 1}")
            while p["id"] in seen:
                p["id"] += "_2"
            p, err = self.check_panel(p)
            if err is None:
                self.log(f"  ✓ {p['id']}")
            else:
                fixed = self._repair(base, p, err)
                if fixed is None:
                    self.warnings.append(f"dropped panel {p['id']!r} after {self.max_retries} failed fixes: {err}")
                    self.log(f"  ✗ {p['id']}: dropped")
                    continue
                p = fixed
                while p["id"] in seen:
                    p["id"] += "_2"
            seen.add(p["id"])
            panels.append(p)
        if not panels:
            raise PlanError("no panel survived validation:\n  " + "\n  ".join(self.warnings))

        filters = []
        for f in data.get("filters") or []:
            if not isinstance(f, dict) or f.get("column") not in self.columns:
                self.warnings.append(f"dropped filter {f!r}: unknown column")
                continue
            ftype = f.get("type") if f.get("type") in FILTER_TYPES else ("date_range" if "date" in str(f.get("type")) else "select")
            filters.append({"column": f["column"], "type": ftype, **({"label": f["label"]} if isinstance(f.get("label"), str) else {})})

        refresh = data.get("refresh_seconds", keep.get("refresh_seconds", 60))
        spec_dict = {
            "title": str(data.get("title") or keep.get("title") or "Dashboard"),
            "description": data.get("description") if isinstance(data.get("description"), str) else keep.get("description"),
            "source": keep.get("source"),
            "refresh_seconds": refresh if isinstance(refresh, int) and refresh >= 0 else 60,
            "model": keep.get("model"),
            "filters": filters,
            "panels": panels,
        }
        return spec_from_dict({k: v for k, v in spec_dict.items() if v is not None})

    # ---- entry points
    def plan(self, request: str, *, source: str | None = None, model_label: str | None = None) -> Spec:
        n = len(self.summary["tables"])
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(request=request, n_tables=n, s="" if n == 1 else "s", schema=schema_for_prompt(self.summary))},
        ]
        self.log("  asking the model for a spec…")
        data, reply = self._ask_for_spec(messages)
        base = messages + [{"role": "assistant", "content": reply}]
        return self.finalize(data, base, keep={"source": source, "model": model_label})

    def edit(self, spec: Spec, change: str, *, model_label: str | None = None) -> Spec:
        current = spec.to_dict()
        shown = {k: v for k, v in current.items() if k not in ("source", "model")}
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": EDIT_TEMPLATE.format(spec=json.dumps(shown, indent=2), schema=schema_for_prompt(self.summary), change=change)},
        ]
        self.log("  asking the model to apply the change…")
        data, reply = self._ask_for_spec(messages)
        base = messages + [{"role": "assistant", "content": reply}]
        keep = {
            "source": spec.source,
            "model": model_label or spec.model,
            "title": spec.title,
            "description": spec.description,
            "refresh_seconds": spec.refresh_seconds,
        }
        return self.finalize(data, base, keep=keep)


def plan(runner: Runner, summary: dict[str, Any], request: str, llm: LLM, **kw: Any) -> tuple[Spec, list[str]]:
    source = kw.pop("source", None)
    model_label = kw.pop("model_label", None)
    planner = Planner(runner, summary, llm, **kw)
    return planner.plan(request, source=source, model_label=model_label), planner.warnings


def edit(runner: Runner, summary: dict[str, Any], spec: Spec, change: str, llm: LLM, **kw: Any) -> tuple[Spec, list[str]]:
    model_label = kw.pop("model_label", None)
    planner = Planner(runner, summary, llm, **kw)
    return planner.edit(spec, change, model_label=model_label), planner.warnings
