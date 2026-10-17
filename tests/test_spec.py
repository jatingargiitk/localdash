import copy
import json

import pytest

from localdash.spec import SpecError, load_spec, result_errors, spec_errors, spec_from_dict

GOOD = {
    "title": "Sales",
    "source": "data/sales.csv",
    "refresh_seconds": 60,
    "filters": [{"column": "plan", "type": "select"}, {"column": "created_at", "type": "date_range"}],
    "panels": [
        {"id": "kpi_rev", "title": "Revenue (30d)", "type": "kpi", "sql": "SELECT 1 AS revenue, 2 AS prev_revenue",
         "value": "revenue", "compare": "prev_revenue"},
        {"id": "signups", "title": "Weekly signups", "type": "line", "sql": "SELECT 1", "x": "week", "y": ["signups"], "series": "plan"},
        {"id": "by_plan", "title": "Revenue by plan", "type": "bar", "sql": "SELECT 1", "x": "plan", "y": "revenue"},
        {"id": "mix", "title": "Mix", "type": "pie", "sql": "SELECT 1", "x": "plan", "y": ["revenue"]},
        {"id": "top", "title": "Top customers", "type": "table", "sql": "SELECT 1", "limit": 20},
    ],
}


def test_good_spec_parses():
    spec = spec_from_dict(GOOD)
    assert spec.title == "Sales"
    assert [p.id for p in spec.panels] == ["kpi_rev", "signups", "by_plan", "mix", "top"]
    assert spec.panel("by_plan").y == ["revenue"]  # string coerced to list
    assert spec.filters[1].type == "date_range"


def test_round_trip(tmp_path):
    spec = spec_from_dict(GOOD)
    path = tmp_path / "dash.json"
    spec.save(path)
    again = load_spec(path)
    assert again.to_dict() == spec.to_dict()
    assert json.loads(path.read_text())["panels"][2]["y"] == ["revenue"]


def mutate(fn):
    d = copy.deepcopy(GOOD)
    fn(d)
    return spec_errors(d)


@pytest.mark.parametrize(
    "fn, needle",
    [
        (lambda d: d.pop("title"), "title"),
        (lambda d: d.__setitem__("panels", []), "non-empty"),
        (lambda d: d["panels"][0].pop("value"), "kpi panels need 'value'"),
        (lambda d: d["panels"][1].pop("x"), "need 'x'"),
        (lambda d: d["panels"][1].pop("y"), "need 'y'"),
        (lambda d: d["panels"][1].__setitem__("type", "heatmap"), "type must be one of"),
        (lambda d: d["panels"][2].__setitem__("id", "signups"), "duplicate panel id"),
        (lambda d: d["panels"][2].__setitem__("id", "bad id!"), "id must match"),
        (lambda d: d["panels"][3].__setitem__("y", ["a", "b"]), "exactly one 'y'"),
        (lambda d: d["panels"][4].__setitem__("limit", 0), "positive integer"),
        (lambda d: d["panels"][4].__setitem__("html", "<div>"), "unknown field 'html'"),
        (lambda d: d["panels"][4].pop("sql"), "'sql'"),
        (lambda d: d.__setitem__("refresh_seconds", -5), "refresh_seconds"),
        (lambda d: d["filters"].append({"column": "x", "type": "slider"}), "type must be one of"),
        (lambda d: d.__setitem__("theme", "neon"), "unknown top-level field"),
    ],
)
def test_bad_specs_rejected(fn, needle):
    errs = mutate(fn)
    assert errs, "expected validation errors"
    assert any(needle in e for e in errs), errs


def test_spec_from_dict_raises():
    with pytest.raises(SpecError):
        spec_from_dict({"title": "x", "panels": [{"id": "a", "type": "kpi", "title": "a", "sql": "select 1"}]})


def test_load_spec_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(SpecError):
        load_spec(p)


def test_result_errors():
    spec = spec_from_dict(GOOD)
    kpi = spec.panel("kpi_rev")
    assert result_errors(kpi, ["revenue", "prev_revenue"], 1) == []
    assert any("do not include" in e for e in result_errors(kpi, ["rev"], 1))
    assert any("exactly 1 row" in e for e in result_errors(kpi, ["revenue", "prev_revenue"], 3))
