import csv
import datetime as dt
import sqlite3

import pytest

PLANS = ["free", "pro", "team"]


@pytest.fixture
def csv_path(tmp_path):
    path = tmp_path / "signups.csv"
    start = dt.date(2025, 1, 1)
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["user_id", "created_at", "plan", "revenue"])
        for i in range(60):
            plan = PLANS[i % 3]
            w.writerow([i + 1, (start + dt.timedelta(days=i)).isoformat(), plan, {"free": 0, "pro": 20, "team": 99}[plan]])
    return path


@pytest.fixture
def sqlite_path(tmp_path):
    path = tmp_path / "shop.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, country TEXT)")
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, ordered_at TEXT)")
    con.executemany("INSERT INTO customers VALUES (?, ?, ?)", [(1, "Ada", "UK"), (2, "Linus", "FI"), (3, "Grace", "US")])
    con.executemany(
        "INSERT INTO orders VALUES (?, ?, ?, ?)",
        [(i, (i % 3) + 1, 10.0 * i, f"2025-02-{i:02d}") for i in range(1, 21)],
    )
    con.commit()
    con.close()
    return path
