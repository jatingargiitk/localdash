import json

import pytest

from localdash.planner import PlanError, extract_json, plan, pretty_sql
from localdash.query import Runner
from localdash.schema import introspect
from localdash.sources import connect


class FakeLLM:
    """Replays canned replies and records every conversation it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages):
        self.calls.append([dict(m) for m in messages])
        if not self.replies:
            raise AssertionError("FakeLLM ran out of replies")
        r = self.replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r)


def setup(csv_path):
    src = connect(str(csv_path))
    return Runner(src), introspect(src)


SPEC_WITH_BROKEN_PANEL = {
    "title": "Signups",
    "filters": [{"column": "plan", "type": "select"}, {"column": "nonexistent", "type": "select"}],
    "panels": [
        {"id": "kpi_users", "title": "Users", "type": "kpi", "sql": "SELECT count(*) AS users FROM signups", "value": "users"},
        {"id": "by_plan", "title": "Revenue by plan", "type": "bar", "x": "plan", "y": ["revenue"],
         "sql": "SELECT plan, sum(revenu) AS revenue FROM signups GROUP BY 1"},  # typo: revenu
    ],
}
FIXED_PANEL = {"id": "by_plan", "title": "Revenue by plan", "type": "bar", "x": "plan", "y": ["revenue"],
               "sql": "SELECT plan, sum(revenue) AS revenue FROM signups GROUP BY 1 ORDER BY 2 DESC"}


def test_retry_fixes_broken_sql(csv_path):
    runner, summary = setup(csv_path)
    llm = FakeLLM(SPEC_WITH_BROKEN_PANEL, FIXED_PANEL)
    spec, warnings = plan(runner, summary, "revenue by plan", llm, source="signups.csv", model_label="fake/model")

    assert [p.id for p in spec.panels] == ["kpi_users", "by_plan"]
    assert "sum(revenue)" in spec.panel("by_plan").sql
    assert spec.source == "signups.csv" and spec.model == "fake/model"
    assert [f.column for f in spec.filters] == ["plan"]  # unknown filter column dropped
    assert any("nonexistent" in w for w in warnings)

    assert len(llm.calls) == 2
    retry_msg = llm.calls[1][-1]["content"]
    assert "by_plan" in retry_msg and "sum(revenu)" in retry_msg
    assert "revenu" in retry_msg.split("Error:", 1)[1]  # the DuckDB error names the bad column
    assert "Fix only this panel" in retry_msg
    # the retry conversation carries the original request + the model's first answer
    assert llm.calls[1][0]["role"] == "system" and llm.calls[1][2]["role"] == "assistant"


def test_panel_dropped_after_max_retries(csv_path):
    runner, summary = setup(csv_path)
    broken = SPEC_WITH_BROKEN_PANEL["panels"][1]
    llm = FakeLLM(SPEC_WITH_BROKEN_PANEL, broken, broken, broken)
    spec, warnings = plan(runner, summary, "x", llm, max_retries=3)
    assert [p.id for p in spec.panels] == ["kpi_users"]
    assert any("dropped panel 'by_plan'" in w for w in warnings)
    assert len(llm.calls) == 4  # 1 plan + 3 retries


def test_unambiguous_column_mismatch_is_bound_without_retry(csv_path):
    runner, summary = setup(csv_path)
    bad = {"title": "t", "panels": [
        {"id": "trend", "title": "Trend", "type": "line", "x": "week", "y": ["signups"],
         "sql": "SELECT date_trunc('week', created_at) AS wk, count(*) AS signups FROM signups GROUP BY 1"},
        {"id": "kpi_rev", "title": "Revenue", "type": "kpi", "sql": "SELECT sum(revenue) AS total FROM signups"},
    ]}
    llm = FakeLLM(bad)
    spec, _ = plan(runner, summary, "trend", llm)
    assert spec.panel("trend").x == "wk"
    assert spec.panel("kpi_rev").value == "total"
    assert len(llm.calls) == 1


def test_kpi_with_many_rows_is_retried(csv_path):
    runner, summary = setup(csv_path)
    bad = {"title": "t", "panels": [{"id": "k", "title": "K", "type": "kpi", "value": "n",
                                     "sql": "SELECT plan, count(*) AS n FROM signups GROUP BY 1"}]}
    good = {"id": "k", "title": "K", "type": "kpi", "value": "n", "sql": "SELECT count(*) AS n FROM signups"}
    llm = FakeLLM(bad, good)
    spec, _ = plan(runner, summary, "x", llm)
    assert spec.panel("k").sql == "SELECT count(*) AS n FROM signups"
    msg = llm.calls[1][-1]["content"]
    assert "exactly 1 row" in msg and 'kpi needs "value"' in msg


def test_invalid_json_is_retried(csv_path):
    runner, summary = setup(csv_path)
    good = {"title": "t", "panels": [SPEC_WITH_BROKEN_PANEL["panels"][0]]}
    llm = FakeLLM("Sure! Here is your dashboard: {oops", "```json\n" + json.dumps(good) + "\n```")
    spec, _ = plan(runner, summary, "x", llm)
    assert spec.panels[0].id == "kpi_users"
    assert "not usable" in llm.calls[1][-1]["content"]


def test_all_panels_fail_raises(csv_path):
    runner, summary = setup(csv_path)
    only_broken = {"title": "t", "panels": [SPEC_WITH_BROKEN_PANEL["panels"][1]]}
    broken = SPEC_WITH_BROKEN_PANEL["panels"][1]
    with pytest.raises(PlanError):
        plan(runner, summary, "x", FakeLLM(only_broken, broken), max_retries=1)


def test_prompt_contains_schema_and_request(csv_path):
    runner, summary = setup(csv_path)
    llm = FakeLLM({"title": "t", "panels": [SPEC_WITH_BROKEN_PANEL["panels"][0]]})
    plan(runner, summary, "weekly signups by plan", llm)
    system, user = llm.calls[0][0]["content"], llm.calls[0][1]["content"]
    assert "DuckDB" in system and "ONLY" in system
    assert "weekly signups by plan" in user and "TABLE signups" in user and "created_at: DATE" in user


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Here:\n```json\n{"a": {"b": "}"}}\n```\nthanks') == {"a": {"b": "}"}}
    assert extract_json('blah {"a": [1, 2]} trailing') == {"a": [1, 2]}
    assert extract_json('<think>hmm {no}</think>{"a": 2}') == {"a": 2}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_pretty_sql_only_touches_whitespace():
    sql = "SELECT plan, count(*) AS n FROM t WHERE x IN (SELECT y FROM u WHERE z = 'a from b') GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
    out = pretty_sql(sql)
    assert out.split() == sql.split()
    assert out.splitlines()[1].startswith("FROM t")
    assert "'a from b'" in out and "(SELECT y FROM u" in out
