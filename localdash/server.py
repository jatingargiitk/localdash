"""``localdash serve``: a tiny local HTTP server for live dashboards.

Routes
  GET /                    the dashboard (data embedded, then kept fresh)
  GET /data/<panel_id>     re-runs one panel's SQL, returns JSON
  GET /data                every panel at once
  GET /spec                the current spec JSON
Filter values travel as query params: ``?f.plan=pro&f.created_at.from=2025-01-01``.

Sources are DuckDB views, so each request re-reads the CSV/Parquet/database:
edit the file and the next refresh shows it. Editing the spec file is picked
up too; the page reloads itself when the spec changes.
"""

from __future__ import annotations

import hashlib
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .query import Runner
from .render import chartjs_source, render_html, run_all, run_panel
from .sources import Source, connect
from .spec import Spec, load_spec


CHARTJS_ROUTE = "/static/chart.umd.min.js"


class Dashboard:
    """Holds the spec + connection and reloads them when the spec file changes."""

    def __init__(self, spec_path: str | Path, *, theme: str = "dark", offline: bool = False,
                 source_override: str | None = None, refresh_override: int | None = None):
        self.spec_path = Path(spec_path).resolve()
        self.theme = theme
        self.offline = offline
        self.source_override = source_override
        self.refresh_override = refresh_override
        self._lock = threading.Lock()
        self.spec: Spec
        self.source: Source | None = None
        self.runner: Runner
        self.version = ""
        self.last_error: str | None = None
        self._load()

    def _load(self) -> None:
        text = self.spec_path.read_text()
        spec = load_spec(self.spec_path)
        if self.refresh_override is not None:
            spec.refresh_seconds = self.refresh_override
        source = self.source_override or spec.source
        if not source:
            raise ValueError("the spec has no 'source'; pass --source")
        new_source = connect(source, base_dir=self.spec_path.parent)
        old = self.source
        self.spec, self.source, self.runner = spec, new_source, Runner(new_source)
        self.version = hashlib.sha1(text.encode()).hexdigest()[:12]
        if old is not None:
            old.close()

    def maybe_reload(self) -> None:
        with self._lock:
            try:
                text = self.spec_path.read_text()
            except OSError:
                return
            if hashlib.sha1(text.encode()).hexdigest()[:12] == self.version:
                return
            try:
                self._load()
                self.last_error = None
            except Exception as e:  # keep serving the last good spec
                self.last_error = f"spec reload failed: {e}"

    @property
    def filter_defs(self) -> list[dict[str, str]]:
        return [{"column": f.column, "type": f.type} for f in self.spec.filters]

    def parse_filters(self, query: dict[str, list[str]]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for f in self.spec.filters:
            if f.type == "select":
                v = query.get(f"f.{f.column}", [None])[0]
                if v:
                    values[f.column] = v
            else:
                lo = query.get(f"f.{f.column}.from", [None])[0]
                hi = query.get(f"f.{f.column}.to", [None])[0]
                if lo or hi:
                    values[f.column] = {"from": lo, "to": hi}
        return values

    def page(self) -> str:
        self.maybe_reload()
        results = run_all(self.runner, self.spec)
        try:
            options = self.runner.filter_options(self.filter_defs)
        except Exception:
            options = {}
        return render_html(self.spec, results, theme=self.theme, offline=self.offline, mode="serve",
                           filter_options=options, spec_version=self.version,
                           chartjs_url=None if self.offline else CHARTJS_ROUTE)

    def panel_data(self, panel_id: str, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        self.maybe_reload()
        panel = self.spec.panel(panel_id)
        if panel is None:
            return 404, {"error": f"no panel {panel_id!r}", "spec_version": self.version}
        res = run_panel(self.runner, panel, self.filter_defs, self.parse_filters(query))
        res["spec_version"] = self.version
        return 200, res

    def all_data(self, query: dict[str, list[str]]) -> dict[str, Any]:
        self.maybe_reload()
        values = self.parse_filters(query)
        return {
            "spec_version": self.version,
            "panels": {p.id: run_panel(self.runner, p, self.filter_defs, values) for p in self.spec.panels},
        }

    def close(self) -> None:
        if self.source:
            self.source.close()


def make_handler(dash: Dashboard, quiet: bool = True):
    class Handler(BaseHTTPRequestHandler):
        server_version = "localdash"

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, default=str).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            query = parse_qs(url.query)
            path = url.path.rstrip("/") or "/"
            try:
                if path in ("/", "/index.html"):
                    self._send(200, dash.page().encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/data":
                    self._json(200, dash.all_data(query))
                elif path.startswith("/data/"):
                    status, data = dash.panel_data(unquote(path[len("/data/"):]), query)
                    self._json(status, data)
                elif path == CHARTJS_ROUTE:
                    self._send(200, chartjs_source().encode("utf-8"), "application/javascript")
                elif path == "/spec":
                    self._json(200, dash.spec.to_dict())
                elif path == "/healthz":
                    self._json(200, {"ok": True, "spec_version": dash.version, "error": dash.last_error})
                else:
                    self._json(404, {"error": "not found"})
            except BrokenPipeError:
                pass
            except Exception as e:
                self._json(500, {"error": str(e)})

        def log_message(self, fmt: str, *args: Any) -> None:
            if not quiet:
                super().log_message(fmt, *args)

    return Handler


def serve(spec_path: str | Path, *, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          theme: str = "dark", offline: bool = False, source_override: str | None = None,
          refresh_override: int | None = None, quiet: bool = True, on_ready=None) -> None:
    dash = Dashboard(spec_path, theme=theme, offline=offline, source_override=source_override,
                     refresh_override=refresh_override)
    httpd = None
    for candidate in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer((host, candidate), make_handler(dash, quiet=quiet))
            break
        except OSError:
            continue
    if httpd is None:
        raise OSError(f"no free port in {port}-{port + 19}")
    httpd.daemon_threads = True
    # not "localhost": browsers try ::1 first and may reach a different server on the same port
    url = f"http://{'127.0.0.1' if host in ('127.0.0.1', '0.0.0.0', 'localhost') else host}:{httpd.server_address[1]}/"
    if on_ready:
        on_ready(url, dash)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        dash.close()
