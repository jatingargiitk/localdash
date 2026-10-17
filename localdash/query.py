"""Run panel SQL against a Source, with optional dashboard filters.

Filter approach (base-table shadowing): every active filter is pushed down to
the *base tables* that contain the filtered column, via CTEs that shadow those
tables by name::

    WITH "saas" AS (SELECT * FROM main."saas" WHERE "plan" = 'pro')
    SELECT * FROM (<panel sql>) AS _ld

Because a CTE takes precedence over a table of the same name, the panel's own
SQL is untouched and every panel that reads ``saas`` (KPIs, trends, tables)
sees the filtered rows, even when its result no longer contains the column.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
import threading
import uuid
from typing import Any

from .sources import Source, quote_ident, quote_literal

DEFAULT_ROW_CAP = 5000


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 2**53 else str(value)  # JS numbers lose precision past 2^53
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, decimal.Decimal):
        f = float(value)
        return int(f) if f.is_integer() and abs(f) < 2**53 else f
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0):
            return value.date().isoformat()
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, (uuid.UUID,)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return str(value)


def strip_sql(sql: str) -> str:
    sql = sql.strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


def filter_columns(source: Source) -> dict[str, list[str]]:
    """Map each base table to its column names."""
    out: dict[str, list[str]] = {}
    for table in source.tables:
        rows = source.con.execute(f"DESCRIBE {quote_ident(table)}").fetchall()
        out[table] = [r[0] for r in rows]
    return out


def build_where(filters: list[dict], values: dict[str, Any], columns: list[str]) -> list[str]:
    """Translate active filter values into WHERE clauses for a table with ``columns``."""
    clauses: list[str] = []
    for f in filters:
        col = f["column"]
        if col not in columns:
            continue
        v = values.get(col)
        if v in (None, "", [], {}):
            continue
        qcol = quote_ident(col)
        if f["type"] == "select":
            items = v if isinstance(v, list) else [v]
            items = [i for i in items if i not in (None, "")]
            if items:
                in_list = ", ".join(quote_literal(i) for i in items)
                clauses.append(f"CAST({qcol} AS VARCHAR) IN ({in_list})")
        elif f["type"] == "date_range" and isinstance(v, dict):
            if v.get("from"):
                clauses.append(f"CAST({qcol} AS DATE) >= CAST({quote_literal(v['from'])} AS DATE)")
            if v.get("to"):
                clauses.append(f"CAST({qcol} AS DATE) <= CAST({quote_literal(v['to'])} AS DATE)")
    return clauses


def wrap_with_filters(
    sql: str,
    source: Source,
    filters: list[dict] | None,
    values: dict[str, Any] | None,
    table_columns: dict[str, list[str]] | None = None,
) -> str:
    sql = strip_sql(sql)
    if not filters or not values:
        return sql
    table_columns = table_columns or filter_columns(source)
    ctes = []
    for table, cols in table_columns.items():
        clauses = build_where(filters, values, cols)
        if clauses:
            origin = source.origins.get(table, f"main.{quote_ident(table)}")
            ctes.append(f"{quote_ident(table)} AS (SELECT * FROM {origin} WHERE {' AND '.join(clauses)})")
    if not ctes:
        return sql
    return f"WITH {', '.join(ctes)}\nSELECT * FROM (\n{sql}\n) AS _ld"


class Runner:
    """Thread-safe query runner bound to one Source."""

    def __init__(self, source: Source):
        self.source = source
        self._lock = threading.Lock()
        self._table_columns: dict[str, list[str]] | None = None

    @property
    def table_columns(self) -> dict[str, list[str]]:
        if self._table_columns is None:
            self._table_columns = filter_columns(self.source)
        return self._table_columns

    def run(
        self,
        sql: str,
        *,
        limit: int | None = DEFAULT_ROW_CAP,
        filters: list[dict] | None = None,
        values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute ``sql`` and return ``{"columns": [...], "rows": [[...]], "truncated": bool}``."""
        with self._lock:
            q = wrap_with_filters(sql, self.source, filters, values, self.table_columns if filters else None)
            if limit is not None:
                q = f"SELECT * FROM (\n{q}\n) AS _lim LIMIT {int(limit) + 1}"
            cur = self.source.con.execute(q)
            columns = [d[0] for d in cur.description]
            raw = cur.fetchall()
        truncated = limit is not None and len(raw) > limit
        if truncated:
            raw = raw[:limit]
        rows = [[to_jsonable(v) for v in r] for r in raw]
        return {"columns": columns, "rows": rows, "truncated": truncated}

    def filter_options(self, filters: list[dict]) -> dict[str, Any]:
        """Distinct values (select) or min/max (date_range) for each filter column."""
        out: dict[str, Any] = {}
        for f in filters:
            col = f["column"]
            tables = [t for t, cols in self.table_columns.items() if col in cols]
            if not tables:
                continue
            union = " UNION ALL ".join(
                f"SELECT {quote_ident(col)} AS v FROM {quote_ident(t)}" for t in tables
            )
            with self._lock:
                if f["type"] == "select":
                    rows = self.source.con.execute(
                        f"SELECT DISTINCT CAST(v AS VARCHAR) FROM ({union}) WHERE v IS NOT NULL ORDER BY 1 LIMIT 200"
                    ).fetchall()
                    out[col] = {"values": [r[0] for r in rows]}
                else:
                    lo, hi = self.source.con.execute(
                        f"SELECT CAST(min(v) AS DATE), CAST(max(v) AS DATE) FROM ({union})"
                    ).fetchone()
                    out[col] = {"min": to_jsonable(lo), "max": to_jsonable(hi)}
        return out
