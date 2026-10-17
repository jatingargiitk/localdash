---
name: localdash
description: Build a local HTML dashboard from a CSV, Parquet, JSON, SQLite, DuckDB or Postgres source with localdash. Use when the user asks for a dashboard, charts, KPIs or a visual overview of a data file or database. You write the dashboard spec yourself, so no model API key is needed.
---

# localdash: dashboards from data, every chart shows its SQL

You (Claude) act as the planner: read the schema, write a JSON spec, validate
it, render it. localdash runs the SQL locally in DuckDB and renders the HTML
deterministically. Never write HTML yourself, only the spec.

## 0. Check the tool

```bash
localdash --version || pip install localdash
```

If `pip install` is not possible, ask the user how they want it installed (for
example `pipx install localdash` or `uv tool install localdash`).

## 1. Read the schema

```bash
localdash schema <source>
```

`<source>` is a CSV/Parquet/JSON file, a directory of them, a `.sqlite`/`.db`
or `.duckdb` file, or a `postgres://user@host/db` URL. The output lists tables
(each file becomes a table named after it), columns, types, row counts,
min/max of numeric and date columns, common values and sample rows.

To check an idea before writing it into the spec:

```bash
localdash query <source> "SELECT plan, count(*) FROM saas GROUP BY 1"
```

## 2. Write the spec

Save it as `<name>.json`, next to where the HTML should go. Make `source` a path
**relative to the spec file**. Set `"model": "claude-code"`.

```json
{
  "title": "SaaS health",
  "description": "One sentence on what this shows.",
  "source": "data/saas.csv",
  "refresh_seconds": 60,
  "model": "claude-code",
  "filters": [{"column": "plan", "type": "select"}, {"column": "signup_date", "type": "date_range"}],
  "panels": [
    {"id": "kpi_mrr", "title": "MRR", "type": "kpi", "format": "currency", "value": "mrr", "compare": "mrr_30d_ago",
     "sql": "WITH a AS (SELECT max(signup_date) AS d FROM saas)\nSELECT\n  sum(mrr) FILTER (WHERE churned_at IS NULL) AS mrr,\n  sum(mrr) FILTER (WHERE signup_date <= d - INTERVAL 30 DAY AND (churned_at IS NULL OR churned_at > d - INTERVAL 30 DAY)) AS mrr_30d_ago\nFROM saas, a"},
    {"id": "weekly_signups", "title": "Weekly signups by plan", "type": "stacked_bar", "x": "week", "y": ["signups"], "series": "plan",
     "sql": "SELECT date_trunc('week', signup_date) AS week, plan, count(*) AS signups\nFROM saas\nGROUP BY 1, 2\nORDER BY 1"},
    {"id": "top", "title": "Top accounts", "type": "table", "limit": 20,
     "sql": "SELECT company, plan, mrr\nFROM saas\nWHERE churned_at IS NULL\nORDER BY mrr DESC\nLIMIT 20"}
  ]
}
```

Panel types and the fields they need:

| type | needs | notes |
|---|---|---|
| `kpi` | `value` | SQL returns exactly 1 row. Optional `compare` (previous period column) and `lower_is_better: true` for churn/cost. |
| `line`, `area`, `bar`, `stacked_bar` | `x`, `y` (list) | Optional `series`: a column whose values become separate lines/bars (long format). Multi-series areas stack. |
| `scatter` | `x`, `y` | Numeric x and y; optional `series`. |
| `pie` | `x` (label), `y` (one column) | 8 slices or fewer. Rendered as a donut with the total in the middle. |
| `table` | nothing extra | Optional `limit` (default 50). |

Optional on any panel: `format` (`currency`, `percent` for a 0-1 ratio,
`compact`, `number`), `description`, `span` (1 = half width, 2 = full width).
Filters: `select` for low-cardinality text, `date_range` for dates. They are
applied to every base table containing that column (in `localdash serve`).
Run `localdash spec-schema` for the full JSON Schema.

Rules that make good dashboards:
- DuckDB SQL only, and only tables/columns from the schema output.
- `x`/`y`/`series`/`value`/`compare` must exactly match SELECT aliases.
- 4-8 panels: 1-4 KPIs, then trends, then breakdowns, then one table.
- Time series: `date_trunc('day'|'week'|'month', col)`, `ORDER BY` time.
- Anchor "last 30 days" to the data, `(SELECT max(d) FROM t)`, never `current_date`.
- Never `SELECT *` on big tables; tables pick columns, `ORDER BY`, `LIMIT`.
- Write SQL on multiple lines (`\n`); it is shown to the user under each chart.

## 3. Validate until clean

```bash
localdash validate <name>.json
```

Prints JSON with `ok`, structural `spec_errors` and per-panel results (the
DuckDB error, or the result columns and first rows). Exit code 2 means
something failed: fix only the failing panels and run it again. Also look at
`first_rows`: a KPI showing 0 or a chart with one point usually means a wrong
date window or join, even when the SQL ran fine.

## 4. Render

```bash
localdash render <name>.json -o <name>.html            # Chart.js from CDN
localdash render <name>.json -o <name>.html --offline  # fully self-contained
localdash render <name>.json --theme light
```

Tell the user the path and offer to open it (`open <name>.html` on macOS). For
a live view that re-runs queries on a timer and enables filters:

```bash
localdash serve <name>.json --port 8765
```

## 5. Changes

For "make signups a bar chart" or "add a churn panel", edit the spec JSON
directly, then validate and render again. Keep untouched panels exactly as
they are.
