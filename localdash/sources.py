"""Connect to a data source through DuckDB.

Every source ends up as one or more *views* in DuckDB's in-memory ``main``
schema. Views (not copies) mean every query re-reads the underlying file or
database, which is what makes ``localdash serve`` pick up changes live.

Supported sources:
  * CSV / TSV / Parquet / JSON / NDJSON files, globs, or a directory of them
  * SQLite files (.sqlite, .sqlite3, .db) or ``sqlite:///path``
  * DuckDB files (.duckdb, .ddb)
  * Postgres: ``postgres://...`` / ``postgresql://...`` or a libpq string
    prefixed with ``postgres:`` (e.g. ``postgres:dbname=shop host=localhost``)
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

FILE_READERS = {
    ".csv": "read_csv_auto",
    ".tsv": "read_csv_auto",
    ".txt": "read_csv_auto",
    ".parquet": "read_parquet",
    ".pq": "read_parquet",
    ".json": "read_json_auto",
    ".jsonl": "read_json_auto",
    ".ndjson": "read_json_auto",
}
SQLITE_EXTS = {".sqlite", ".sqlite3", ".db"}
DUCKDB_EXTS = {".duckdb", ".ddb"}


class SourceError(Exception):
    pass


@dataclass
class Source:
    con: duckdb.DuckDBPyConnection
    tables: list[str]
    kind: str
    location: str
    # view name -> fully qualified original relation, used to shadow tables with filters
    origins: dict[str, str] = field(default_factory=dict)

    def close(self) -> None:
        self.con.close()


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def table_name_for(path: str) -> str:
    stem = Path(path).name
    for ext in sorted(FILE_READERS, key=len, reverse=True):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    name = re.sub(r"[^0-9a-zA-Z_]+", "_", stem).strip("_").lower() or "data"
    if name[0].isdigit():
        name = "t_" + name
    return name


def resolve_source(source: str, base_dir: str | os.PathLike | None = None) -> str:
    """Resolve a relative file source against ``base_dir`` (e.g. the spec's folder)."""
    if is_postgres(source) or source.startswith("sqlite:"):
        return source
    if os.path.isabs(source) or base_dir is None:
        return source
    candidate = os.path.join(base_dir, source)
    if glob.glob(candidate) or os.path.exists(candidate):
        return candidate
    return source


def is_postgres(source: str) -> bool:
    return source.startswith(("postgres://", "postgresql://", "postgres:", "pg:"))


def connect(source: str, base_dir: str | os.PathLike | None = None) -> Source:
    source = resolve_source(source, base_dir)
    con = duckdb.connect(database=":memory:")
    try:
        if is_postgres(source):
            return _connect_attached(con, source, "postgres")
        if source.startswith("sqlite:"):
            path = re.sub(r"^sqlite:(//)?", "", source)
            return _connect_attached(con, path, "sqlite")
        ext = Path(source).suffix.lower()
        if os.path.isdir(source):
            return _connect_files(con, source, _files_in_dir(source))
        if ext in SQLITE_EXTS:
            return _connect_attached(con, source, "sqlite")
        if ext in DUCKDB_EXTS:
            return _connect_attached(con, source, "duckdb")
        if ext in FILE_READERS or any(ch in source for ch in "*?["):
            files = sorted(glob.glob(source)) if any(ch in source for ch in "*?[") else [source]
            return _connect_files(con, source, files, glob_pattern=source if len(files) > 1 else None)
        raise SourceError(
            f"Don't know how to open {source!r}. Use a CSV/Parquet/JSON file, a SQLite/DuckDB file, "
            "a directory of files, or a postgres:// URL."
        )
    except Exception:
        con.close()
        raise


def _files_in_dir(path: str) -> list[str]:
    files = [
        os.path.join(path, f)
        for f in sorted(os.listdir(path))
        if Path(f).suffix.lower() in FILE_READERS
    ]
    if not files:
        raise SourceError(f"No CSV/Parquet/JSON files found in {path}")
    return files


def _connect_files(con, source: str, files: list[str], glob_pattern: str | None = None) -> Source:
    if not files:
        raise SourceError(f"No files match {source!r}")
    tables: list[str] = []
    origins: dict[str, str] = {}
    if glob_pattern:
        # all files matching a glob become one table (e.g. logs/2024-*.parquet)
        groups = {table_name_for(re.sub(r"[*?\[\]]", "", glob_pattern)): (glob_pattern, files[0])}
    else:
        groups = {}
        for f in files:
            if not os.path.exists(f):
                raise SourceError(f"File not found: {f}")
            name = table_name_for(f)
            while name in groups:
                name += "_2"
            groups[name] = (f, f)
    for name, (pattern, sample_file) in groups.items():
        reader = FILE_READERS.get(Path(sample_file).suffix.lower(), "read_csv_auto")
        path = os.path.abspath(pattern)
        con.execute(f"CREATE VIEW {quote_ident(name)} AS SELECT * FROM {reader}({quote_literal(path)})")
        tables.append(name)
        origins[name] = f"main.{quote_ident(name)}"
    return Source(con=con, tables=tables, kind="files", location=source, origins=origins)


def _connect_attached(con, target: str, kind: str) -> Source:
    if kind in ("sqlite", "duckdb") and not os.path.exists(target):
        raise SourceError(f"File not found: {target}")
    if kind == "postgres":
        conn_str = re.sub(r"^(pg|postgres):(?!//)", "", target)
        con.execute("INSTALL postgres; LOAD postgres;")
        con.execute(f"ATTACH {quote_literal(conn_str)} AS src (TYPE postgres, READ_ONLY)")
    elif kind == "sqlite":
        con.execute("INSTALL sqlite; LOAD sqlite;")
        con.execute(f"ATTACH {quote_literal(os.path.abspath(target))} AS src (TYPE sqlite, READ_ONLY)")
    else:
        con.execute(f"ATTACH {quote_literal(os.path.abspath(target))} AS src (READ_ONLY)")

    rows = con.execute(
        """
        SELECT table_schema, table_name FROM information_schema.tables
        WHERE table_catalog = 'src'
          AND table_schema NOT IN ('information_schema', 'pg_catalog')
          AND table_name NOT LIKE 'sqlite_%'
        ORDER BY table_schema, table_name
        """
    ).fetchall()
    if not rows:
        raise SourceError(f"No tables found in {target}")
    default_schemas = {"main", "public"}
    tables: list[str] = []
    origins: dict[str, str] = {}
    for schema, table in rows:
        name = table if schema in default_schemas else f"{schema}_{table}"
        name = re.sub(r"[^0-9a-zA-Z_]+", "_", name)
        qualified = f"src.{quote_ident(schema)}.{quote_ident(table)}"
        con.execute(f"CREATE VIEW {quote_ident(name)} AS SELECT * FROM {qualified}")
        tables.append(name)
        origins[name] = f"main.{quote_ident(name)}"
    location = _redact(target) if kind == "postgres" else target
    return Source(con=con, tables=tables, kind=kind, location=location, origins=origins)


def _redact(conn: str) -> str:
    conn = re.sub(r"(://[^:/@]+):[^@]+@", r"\1:***@", conn)
    return re.sub(r"(password=)\S+", r"\1***", conn)
