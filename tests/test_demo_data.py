"""Tests for src.demo_data: labelling, determinism, isolation from real runs."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.database import Database, new_run_id
from src.demo_data import DEMO_LABEL, clear_demo_runs, generate_demo_runs

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


class FakeEp:
    def __init__(self, id: str, safety_class: str = "read_only", category: str = "Market Data"):
        self.id, self.name, self.safety_class, self.category = id, f"{id} name", safety_class, category
        self.method, self.path = "POST", f"/{id}"


ENDPOINTS = [FakeEp("UserDetails"), FakeEp("Limits"), FakeEp("PlaceOrder", safety_class="state_changing"),
             {"id": "GetQuotes", "name": "Get Quotes", "safety_class": "read_only", "category": "Market Data"}]


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "demo.db")


def test_demo_runs_are_labelled_and_read_only(db):
    run_ids = generate_demo_runs(db, ENDPOINTS, n_runs=2, iterations=30, seed=7, now=NOW)
    assert len(run_ids) == 2
    for rid in run_ids:
        run = db.get_run(rid)
        assert run["run_type"] == "demo" and run["is_synthetic"] is True
        assert run["notes"] == DEMO_LABEL and run["status"] == "completed"
        res = db.get_results([rid], include_warmup=True)
        assert set(res["source"]) == {"synthetic"} and res["is_synthetic"].all()
        assert set(res["endpoint_id"]) == {"UserDetails", "Limits", "GetQuotes"}  # no PlaceOrder
        assert res["is_warmup"].sum() == 2 * 3
        assert len(res) == (30 + 2) * 3
        metrics = db.get_metrics([rid])
        assert set(metrics["endpoint_id"]) == {"UserDetails", "Limits", "GetQuotes"}
        assert (metrics["total"] == 30).all()
        assert set(metrics["category"]) == {"Market Data"}


def test_demo_latencies_realistic_with_failures(db):
    rid = generate_demo_runs(db, ENDPOINTS, n_runs=1, iterations=1000, seed=1, now=NOW)[0]
    res = db.get_results([rid])
    ok = res[res["success"]]
    assert 30 < ok["latency_ms"].median() < 700
    fail_rate = 1 - res["success"].mean()
    assert 0.003 < fail_rate < 0.06
    assert set(res.loc[~res["success"], "outcome"]) <= {"api_error", "http_error", "timeout"}
    assert ok["latency_ms"].max() > 2 * ok["latency_ms"].median()  # long tail present


def test_demo_deterministic_with_seed(tmp_path):
    a = Database(tmp_path / "a.db")
    b = Database(tmp_path / "b.db")
    ra = generate_demo_runs(a, ENDPOINTS, n_runs=1, iterations=20, seed=99, now=NOW)[0]
    rb = generate_demo_runs(b, ENDPOINTS, n_runs=1, iterations=20, seed=99, now=NOW)[0]
    la = a.get_results([ra])["latency_ms"].tolist()
    lb = b.get_results([rb])["latency_ms"].tolist()
    assert la == lb
    assert a.get_run(ra)["started_at"] == b.get_run(rb)["started_at"]


def test_demo_runs_spread_over_days(db):
    run_ids = generate_demo_runs(db, ENDPOINTS, n_runs=3, iterations=5, now=NOW)
    days = {db.get_run(r)["started_at"][:10] for r in run_ids}
    assert len(days) == 3
    assert all(d < "2026-10-16" for d in days)


def test_defaults_when_no_read_only_endpoints(db):
    rid = generate_demo_runs(db, [FakeEp("PlaceOrder", "state_changing")], n_runs=1, iterations=5, now=NOW)[0]
    assert "PlaceOrder" not in set(db.get_results([rid])["endpoint_id"])
    assert len(db.get_results([rid])) > 0


def test_clear_demo_runs_keeps_real_runs(db):
    real = db.create_run(run_id=new_run_id(), run_type="http", environment="mock", base_host="h",
                         connection_mode="warm", config={}, planned_requests=1)
    db.insert_results([{"run_id": real, "endpoint_id": "Limits", "seq": 1, "latency_ms": 10.0, "success": True,
                        "outcome": "success", "source": "measured", "started_at": "2026-10-16T00:00:00Z",
                        "completed_at": "2026-10-16T00:00:00.01Z"}])
    generate_demo_runs(db, ENDPOINTS, n_runs=2, iterations=5, now=NOW)
    assert len(db.list_runs()) == 3
    assert db.list_runs(include_synthetic=False)["run_id"].tolist() == [real]
    assert len(db.get_results(include_synthetic=False)) == 1
    assert clear_demo_runs(db) == 2
    assert db.list_runs()["run_id"].tolist() == [real]
    assert len(db.get_results()) == 1
    assert clear_demo_runs(db) == 0
