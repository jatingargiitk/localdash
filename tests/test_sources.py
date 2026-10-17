import json

from typer.testing import CliRunner

from localdash.cli import app
from localdash.query import Runner
from localdash.schema import introspect, schema_for_prompt
from localdash.sources import connect, table_name_for


def test_csv_loads_and_introspects(csv_path):
    src = connect(str(csv_path))
    assert src.tables == ["signups"]
    summary = introspect(src)
    t = summary["tables"][0]
    assert t["row_count"] == 60
    cols = {c["name"]: c for c in t["columns"]}
    assert set(cols) == {"user_id", "created_at", "plan", "revenue"}
    assert cols["created_at"]["type"] == "DATE"
    assert cols["created_at"]["min"] == "2025-01-01"
    assert cols["revenue"]["max"] == 99
    assert set(cols["plan"]["top_values"]) == {"free", "pro", "team"}
    assert len(t["sample_rows"]) == 5
    assert "TABLE signups" in schema_for_prompt(summary)


def test_csv_no_samples(csv_path):
    summary = introspect(connect(str(csv_path)), samples=False)
    t = summary["tables"][0]
    assert "sample_rows" not in t
    assert all("top_values" not in c for c in t["columns"])


def test_sqlite_loads_all_tables(sqlite_path):
    src = connect(str(sqlite_path))
    assert src.kind == "sqlite"
    assert sorted(src.tables) == ["customers", "orders"]
    summary = introspect(src)
    counts = {t["name"]: t["row_count"] for t in summary["tables"]}
    assert counts == {"customers": 3, "orders": 20}
    res = Runner(src).run(
        "SELECT c.country, sum(o.amount) AS total FROM orders o JOIN customers c ON c.id = o.customer_id "
        "GROUP BY 1 ORDER BY total DESC"
    )
    assert res["columns"] == ["country", "total"]
    assert len(res["rows"]) == 3


def test_sqlite_url_form(sqlite_path):
    src = connect(f"sqlite:///{sqlite_path}")
    assert "orders" in src.tables


def test_filters_shadow_base_table(csv_path):
    runner = Runner(connect(str(csv_path)))
    filters = [{"column": "plan", "type": "select"}, {"column": "created_at", "type": "date_range"}]
    sql = "SELECT count(*) AS n, sum(revenue) AS rev FROM signups"
    assert runner.run(sql)["rows"] == [[60, 2380]]
    assert runner.run(sql, filters=filters, values={"plan": "pro"})["rows"] == [[20, 400]]
    res = runner.run(sql, filters=filters, values={"created_at": {"from": "2025-01-01", "to": "2025-01-10"}})
    assert res["rows"][0][0] == 10


def test_live_reread(csv_path):
    runner = Runner(connect(str(csv_path)))
    assert runner.run("SELECT count(*) FROM signups")["rows"] == [[60]]
    with open(csv_path, "a") as f:
        f.write("61,2025-03-02,pro,20\n")
    assert runner.run("SELECT count(*) FROM signups")["rows"] == [[61]]


def test_limit_preserves_order(csv_path):
    runner = Runner(connect(str(csv_path)))
    res = runner.run("SELECT user_id FROM signups ORDER BY user_id DESC", limit=3)
    assert [r[0] for r in res["rows"]] == [60, 59, 58]
    assert res["truncated"]


def test_table_name_for():
    assert table_name_for("data/My Sales-2024.csv") == "my_sales_2024"
    assert table_name_for("2024.parquet") == "t_2024"


def test_schema_cli(csv_path):
    result = CliRunner().invoke(app, ["schema", str(csv_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["tables"][0]["name"] == "signups"
