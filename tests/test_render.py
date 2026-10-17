import markupsafe
import json
import re
from html.parser import HTMLParser

from typer.testing import CliRunner

from localdash.cli import app
from localdash.query import Runner
from localdash.render import CHARTJS_CDN, render_html, run_all
from localdash.sources import connect
from localdash.spec import spec_from_dict

VOID = {"meta", "br", "img", "input", "hr", "link", "source", "area", "base", "col", "embed", "param", "track", "wbr", "path", "ellipse", "circle"}


class TagChecker(HTMLParser):
    """Minimal well-formedness check: every non-void tag is closed in order."""

    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}>, open: {self.stack[-3:]}")
        else:
            self.stack.pop()


def make_spec(csv_path):
    return spec_from_dict({
        "title": "Signups <test>",
        "source": str(csv_path),
        "filters": [{"column": "plan", "type": "select"}],
        "panels": [
            {"id": "kpi_users", "title": "Users", "type": "kpi", "sql": "SELECT count(*) AS users FROM signups", "value": "users"},
            {"id": "daily", "title": "Daily", "type": "line", "x": "day", "y": ["n"], "series": "plan",
             "sql": "SELECT created_at AS day, plan, count(*) AS n FROM signups GROUP BY 1, 2 ORDER BY 1"},
            {"id": "by_plan", "title": "By plan", "type": "bar", "x": "plan", "y": ["revenue"],
             "sql": "SELECT plan, sum(revenue) AS revenue FROM signups WHERE plan <> 'x' GROUP BY 1"},
            {"id": "mix", "title": "Mix", "type": "pie", "x": "plan", "y": ["n"],
             "sql": "SELECT plan, count(*) AS n FROM signups GROUP BY 1"},
            {"id": "rows", "title": "Rows", "type": "table", "limit": 5, "sql": "SELECT * FROM signups ORDER BY user_id"},
            {"id": "broken", "title": "Broken", "type": "bar", "x": "a", "y": ["b"], "sql": "SELECT a, b FROM nope"},
        ],
    })


def test_render_contains_every_panel_and_sql(csv_path):
    spec = make_spec(csv_path)
    runner = Runner(connect(str(csv_path)))
    results = run_all(runner, spec)
    assert results["broken"]["error"]
    assert results["rows"]["truncated"] and len(results["rows"]["rows"]) == 5
    out = render_html(spec, results)

    checker = TagChecker()
    checker.feed(out)
    assert not checker.errors, checker.errors[:5]
    assert checker.stack == [], checker.stack
    assert out.lower().startswith("<!doctype html>")

    for p in spec.panels:
        assert f'id="panel-{p.id}"' in out
        assert str(markupsafe.escape(p.sql)) in out  # visible SQL block (escaped)

    payload = json.loads(re.search(r'<script id="ld-data" type="application/json">(.*?)</script>', out, re.S).group(1))
    assert [p["id"] for p in payload["panels"]][0] == "kpi_users"  # KPIs first
    by_id = {p["id"]: p for p in payload["panels"]}
    assert by_id["kpi_users"]["result"]["rows"] == [[60]]
    assert by_id["by_plan"]["sql"] == spec.panel("by_plan").sql
    assert "Signups &lt;test&gt;" in out  # title escaped
    assert CHARTJS_CDN in out


def test_offline_inlines_chartjs(csv_path):
    spec = make_spec(csv_path)
    out = render_html(spec, {}, offline=True, theme="light")
    assert CHARTJS_CDN not in out
    assert "Chart.js v4" in out
    assert 'data-theme="light"' in out


def test_render_cli(csv_path, tmp_path):
    spec = make_spec(csv_path)
    spec.panels = [p for p in spec.panels if p.id != "broken"]
    spec_path = tmp_path / "dash.json"
    spec.save(spec_path)
    out = tmp_path / "dash.html"
    result = CliRunner().invoke(app, ["render", str(spec_path), "-o", str(out), "--strict"])
    assert result.exit_code == 0, result.output
    assert "kpi_users" in out.read_text()


def test_validate_cli_reports_failures(csv_path, tmp_path):
    spec = make_spec(csv_path)
    spec_path = tmp_path / "dash.json"
    spec.save(spec_path)
    result = CliRunner().invoke(app, ["validate", str(spec_path)])
    assert result.exit_code == 2
    report = json.loads(result.stdout)
    assert report["panels"]["daily"]["ok"] is True
    assert report["panels"]["broken"]["ok"] is False
    assert "nope" in report["panels"]["broken"]["error"]
