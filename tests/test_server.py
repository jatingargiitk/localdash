import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from localdash.server import Dashboard, make_handler
from localdash.spec import spec_from_dict


@pytest.fixture
def live(csv_path, tmp_path):
    spec = spec_from_dict({
        "title": "Live",
        "source": csv_path.name,
        "refresh_seconds": 5,
        "filters": [{"column": "plan", "type": "select"}, {"column": "created_at", "type": "date_range"}],
        "panels": [
            {"id": "kpi_users", "title": "Users", "type": "kpi", "sql": "SELECT count(*) AS users FROM signups", "value": "users"},
            {"id": "by_plan", "title": "By plan", "type": "bar", "x": "plan", "y": ["n"],
             "sql": "SELECT plan, count(*) AS n FROM signups GROUP BY 1 ORDER BY 1"},
        ],
    })
    spec_path = csv_path.parent / "dash.json"
    spec.save(spec_path)
    dash = Dashboard(spec_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(dash))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, csv_path, spec_path
    httpd.shutdown()
    dash.close()


def get(url):
    with urllib.request.urlopen(url) as r:
        body = r.read().decode()
        return r.status, (json.loads(body) if r.headers["Content-Type"].startswith("application/json") else body)


def test_page_and_panel_data(live):
    base, _, _ = live
    status, page = get(base + "/")
    assert status == 200 and 'id="panel-kpi_users"' in page and '"mode": "serve"' in page
    assert "/static/chart.umd.min.js" in page  # serve mode never needs the CDN
    status, js = get(base + "/static/chart.umd.min.js")
    assert status == 200 and "Chart.js v4" in js
    _, data = get(base + "/data/kpi_users")
    assert data["rows"] == [[60]] and data["spec_version"]


def test_data_updates_when_csv_changes(live):
    base, csv_path, _ = live
    assert get(base + "/data/kpi_users")[1]["rows"] == [[60]]
    with open(csv_path, "a") as f:
        f.write("61,2025-03-02,pro,20\n62,2025-03-03,team,99\n")
    assert get(base + "/data/kpi_users")[1]["rows"] == [[62]]


def test_filters_apply_to_every_panel(live):
    base, _, _ = live
    assert get(base + "/data/kpi_users?f.plan=pro")[1]["rows"] == [[20]]
    assert get(base + "/data/by_plan?f.plan=pro")[1]["rows"] == [["pro", 20]]
    rows = get(base + "/data/kpi_users?f.created_at.from=2025-01-01&f.created_at.to=2025-01-05")[1]["rows"]
    assert rows == [[5]]
    allp = get(base + "/data?f.plan=team")[1]
    assert allp["panels"]["kpi_users"]["rows"] == [[20]]


def test_spec_edits_are_picked_up(live):
    base, _, spec_path = live
    v1 = get(base + "/data/kpi_users")[1]["spec_version"]
    d = json.loads(spec_path.read_text())
    d["panels"][0]["sql"] = "SELECT count(*) * 2 AS users FROM signups"
    spec_path.write_text(json.dumps(d))
    data = get(base + "/data/kpi_users")[1]
    assert data["spec_version"] != v1
    assert data["rows"] == [[120]]


def test_unknown_panel_404(live):
    base, _, _ = live
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/data/nope")
    assert e.value.code == 404
