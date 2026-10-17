"""Introspect a Source: columns, types, row counts, sample rows, ranges.

The resulting summary is the *only* thing about your data that is sent to a
model (and with ``--no-samples`` the sample rows / top values are dropped too).
"""

from __future__ import annotations

from typing import Any

from .query import to_jsonable
from .sources import Source, quote_ident

NUMERIC_PREFIXES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT",
    "UINTEGER", "UBIGINT", "UHUGEINT", "FLOAT", "DOUBLE", "DECIMAL", "REAL", "INT",
)
TEMPORAL_PREFIXES = ("DATE", "TIMESTAMP", "TIME")
TOP_VALUES_MAX_DISTINCT = 12
SAMPLE_ROWS = 5
MAX_STR = 80


def is_numeric(dtype: str) -> bool:
    return dtype.upper().startswith(NUMERIC_PREFIXES)


def is_temporal(dtype: str) -> bool:
    return dtype.upper().startswith(TEMPORAL_PREFIXES)


def _short(v: Any) -> Any:
    v = to_jsonable(v)
    if isinstance(v, str) and len(v) > MAX_STR:
        return v[: MAX_STR - 1] + "…"
    return v


def introspect_table(source: Source, table: str, samples: bool = True) -> dict[str, Any]:
    con = source.con
    qt = quote_ident(table)
    described = con.execute(f"DESCRIBE {qt}").fetchall()
    columns = [{"name": r[0], "type": r[1]} for r in described]
    row_count = con.execute(f"SELECT count(*) FROM {qt}").fetchone()[0]

    # one pass for ranges, null counts and approximate distinct counts
    exprs = []
    for c in columns:
        qc = quote_ident(c["name"])
        exprs.append(f"count(*) - count({qc})")
        exprs.append(f"approx_count_distinct({qc})")
        if is_numeric(c["type"]) or is_temporal(c["type"]):
            exprs.append(f"min({qc})")
            exprs.append(f"max({qc})")
    stats = con.execute(f"SELECT {', '.join(exprs)} FROM {qt}").fetchone() if exprs else ()
    i = 0
    for c in columns:
        c["nulls"] = stats[i]
        c["distinct"] = stats[i + 1]
        i += 2
        if is_numeric(c["type"]) or is_temporal(c["type"]):
            c["min"], c["max"] = _short(stats[i]), _short(stats[i + 1])
            i += 2
        if samples and c["type"].upper().startswith(("VARCHAR", "BOOLEAN", "ENUM")) and 0 < c["distinct"] <= TOP_VALUES_MAX_DISTINCT:
            qc = quote_ident(c["name"])
            top = con.execute(
                f"SELECT {qc}, count(*) AS n FROM {qt} WHERE {qc} IS NOT NULL "
                f"GROUP BY 1 ORDER BY n DESC, 1 LIMIT {TOP_VALUES_MAX_DISTINCT}"
            ).fetchall()
            c["top_values"] = [_short(v) for v, _ in top]

    out: dict[str, Any] = {"name": table, "row_count": row_count, "columns": columns}
    if samples:
        cur = con.execute(f"SELECT * FROM {qt} LIMIT {SAMPLE_ROWS}")
        names = [d[0] for d in cur.description]
        out["sample_rows"] = [dict(zip(names, (_short(v) for v in row))) for row in cur.fetchall()]
    return out


def introspect(source: Source, samples: bool = True) -> dict[str, Any]:
    return {
        "source": source.location,
        "kind": source.kind,
        "dialect": "duckdb",
        "tables": [introspect_table(source, t, samples=samples) for t in source.tables],
    }


def schema_for_prompt(summary: dict[str, Any]) -> str:
    """Compact, model-friendly rendering of a schema summary."""
    lines: list[str] = []
    for t in summary["tables"]:
        lines.append(f"TABLE {t['name']}  ({t['row_count']:,} rows)")
        for c in t["columns"]:
            bits = [f"  - {c['name']}: {c['type']}"]
            if "min" in c:
                bits.append(f"range [{c['min']} .. {c['max']}]")
            if c.get("top_values"):
                bits.append("values: " + ", ".join(map(str, c["top_values"])))
            elif c.get("distinct") is not None:
                bits.append(f"~{c['distinct']:,} distinct")
            if c.get("nulls"):
                bits.append(f"{c['nulls']:,} nulls")
            lines.append("  ".join(bits))
        if t.get("sample_rows"):
            lines.append("  sample rows:")
            for r in t["sample_rows"]:
                lines.append("    " + ", ".join(f"{k}={v}" for k, v in r.items()))
        lines.append("")
    return "\n".join(lines).rstrip()
