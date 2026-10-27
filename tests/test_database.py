"""Tests for src.database: schema, persistence, cascade, settings guard."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

import pandas as pd
import pytest

from src import redaction
from src.database import Database, RESULT_COLUMNS, get_database, new_run_id
from src.metrics import METRIC_COLUMNS


@dataclass
class FakeEndpoint:
    """Minimal stand-in for endpoint_registry.Endpoint."""

    id: str
    name: str
    category: str = "Market Data"
    method: str = "POST"
    path: str = ""
    safety_class: str = "read_only"
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "category": self.category, "method": self.method,
                "path": self.path or f"/{self.id}", "safety_class": self.safety_class}


class FakeResult:
    """Duck-typed RequestResult exposing to_row()."""

    def __init__(self, **row):
        self._row = row

    def to_row(self) -> dict:
        return dict(self._row)


def result_row(run_id: str, seq: int, latency: float | None = 100.0, *, endpoint_id: str = "UserDetails",
               success: bool = True, is_warmup: bool = False, is_timeout: bool = False,
               error_message: str | None = None, source: str = "measured") -> dict:
    return {
        "run_id": run_id, "endpoint_id": endpoint_id, "endpoint_name": endpoint_id, "path": f"/{endpoint_id}",
        "method": "POST", "seq": seq, "is_warmup": is_warmup, "connection_mode": "warm",
        "started_at": f"2026-10-16T10:00:{seq:02d}.000000Z", "completed_at": f"2026-10-16T10:00:{seq:02d}.100000Z",
        "latency_ms": latency, "http_status": 200 if success else 500, "success": success,
        "outcome": "success" if success else ("timeout" if is_timeout else "http_error"),
        "is_timeout": is_timeout, "error_type": None if success else "http_error",
        "error_message": error_message, "response_bytes": 123, "api_stat": "Ok" if success else None,
        "server_timing": None, "source": source,
    }


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "sub" / "bench.db"


@pytest.fixture()
def db(db_path):
    return Database(db_path)


def make_run(db: Database, run_id: str | None = None, *, synthetic: bool = False, run_type: str = "http",
             started_at: str | None = None) -> str:
    rid = run_id or new_run_id()
    return db.create_run(run_id=rid, run_type=run_type, environment="mock", base_host="example.test",
                         connection_mode="warm", config={"iterations": 5, "delay_ms": 0}, planned_requests=5,
                         is_synthetic=synthetic, notes="test", started_at=started_at)


def test_new_run_id_format():
    rid = new_run_id("demo")
    assert re.fullmatch(r"demo-\d{8}-\d{6}-[0-9a-f]{6}", rid)
    assert new_run_id() != new_run_id()


def test_schema_wal_indexes_and_idempotent(db, db_path):
    db.init_schema()
    db.init_schema()
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"endpoints", "benchmark_runs", "request_results", "endpoint_metrics", "ws_samples",
                "app_settings"} <= tables
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_results_run", "idx_results_endpoint", "idx_results_started",
                "idx_results_run_endpoint", "idx_ws_run_metric"} <= indexes
    finally:
        conn.close()


def test_persistence_across_instances(db_path):
    db1 = Database(db_path)
    rid = make_run(db1)
    assert db1.insert_results([result_row(rid, i, 100.0 + i) for i in range(1, 6)]) == 5
    db1.finish_run(rid, "completed")
    db1.recompute_metrics(rid)
    del db1

    db2 = Database(db_path)
    run = db2.get_run(rid)
    assert run is not None and run["status"] == "completed" and run["config"] == {"iterations": 5, "delay_ms": 0}
    assert run["completed_at"] and run["app_version"]
    results = db2.get_results([rid])
    assert len(results) == 5
    assert list(results.columns) == ["id", *RESULT_COLUMNS]
    assert results["success"].map(type).eq(bool).all()
    assert results["latency_ms"].tolist() == [101.0, 102.0, 103.0, 104.0, 105.0]
    m = db2.get_metrics([rid])
    assert len(m) == 1 and m.iloc[0]["samples"] == 5


def test_insert_results_accepts_objects_and_dicts(db):
    rid = make_run(db)
    n = db.insert_results([FakeResult(**result_row(rid, 1)), result_row(rid, 2)])
    assert n == 2
    assert db.insert_results([]) == 0
    df = db.get_results([rid])
    assert df["is_synthetic"].tolist() == [False, False]


def test_get_results_filters(db):
    real = make_run(db)
    demo = make_run(db, synthetic=True, run_type="demo")
    db.insert_results([result_row(real, 1, is_warmup=True), result_row(real, 2), result_row(real, 3, endpoint_id="Limits")])
    db.insert_results([result_row(demo, 1, source="synthetic")])
    assert len(db.get_results([real])) == 2
    assert len(db.get_results([real], include_warmup=True)) == 3
    assert len(db.get_results([real], endpoint_ids=["Limits"])) == 1
    assert len(db.get_results()) == 3
    assert len(db.get_results(include_synthetic=False)) == 2
    assert db.get_results([demo])["is_synthetic"].tolist() == [True]
    assert len(db.get_results([])) == 0


def test_list_runs_newest_first_and_filters(db):
    old = make_run(db, "run-old", started_at="2026-10-01T00:00:00.000000Z")
    new = make_run(db, "run-new", started_at="2026-10-10T00:00:00.000000Z")
    demo = make_run(db, "demo-x", synthetic=True, run_type="demo", started_at="2026-10-05T00:00:00.000000Z")
    runs = db.list_runs()
    assert runs["run_id"].tolist() == [new, demo, old]
    assert db.list_runs(include_synthetic=False)["run_id"].tolist() == [new, old]
    assert db.list_runs(run_types=["demo"])["run_id"].tolist() == [demo]
    assert "request_count" in runs.columns


def test_create_run_validation(db):
    with pytest.raises(ValueError):
        make_run(db, run_type="bogus")
    rid = make_run(db)
    with pytest.raises(ValueError):
        db.finish_run(rid, "weird")
    assert db.get_run("missing") is None


def test_cascade_delete(db):
    rid = make_run(db)
    keep = make_run(db)
    db.insert_results([result_row(rid, 1), result_row(keep, 1)])
    db.recompute_metrics(rid)
    db.insert_ws_samples(rid, [("connect_ms", 12.5, "2026-10-16T10:00:00Z", None)])
    db.delete_run(rid)
    assert db.get_run(rid) is None
    assert len(db.get_results([rid], include_warmup=True)) == 0
    assert len(db.get_metrics([rid])) == 0
    assert len(db.get_ws_samples(rid)) == 0
    assert len(db.get_results([keep])) == 1


def test_metrics_roundtrip_with_warnings(db):
    rid = make_run(db)
    rows = [result_row(rid, i, 50.0 + i) for i in range(1, 11)] + [result_row(rid, 11, 5.0, success=False)]
    computed = db.recompute_metrics(rid)
    assert len(computed) == 0  # nothing inserted yet
    db.insert_results(rows)
    db.upsert_endpoints([FakeEndpoint("UserDetails", "User Details", category="Authentication")])
    computed = db.recompute_metrics(rid)
    assert list(computed.columns) == METRIC_COLUMNS
    stored = db.get_metrics([rid])
    row = stored.iloc[0]
    assert row["category"] == "Authentication"
    assert row["total"] == 11 and row["failed"] == 1 and row["samples"] == 10
    assert isinstance(row["warnings"], list) and any("P99" in w for w in row["warnings"])
    assert row["p99_reliable"] is False or row["p99_reliable"] == False  # noqa: E712
    assert row["p50_ms"] == pytest.approx(55.5)
    # re-saving replaces rather than duplicates
    db.recompute_metrics(rid)
    assert len(db.get_metrics([rid])) == 1


def test_upsert_endpoints_updates(db):
    db.upsert_endpoints([FakeEndpoint("Limits", "Limits")])
    db.upsert_endpoints([FakeEndpoint("Limits", "Limits v2"), {"id": "Holdings", "name": "Holdings"}])
    eps = db.list_endpoints().set_index("id")
    assert eps.loc["Limits", "name"] == "Limits v2"
    assert "Holdings" in eps.index


def test_error_messages_redacted_on_insert(db):
    redaction.register_secrets(["supersecrettokenvalue123"])
    rid = make_run(db)
    db.insert_results([result_row(rid, 1, success=False,
                                  error_message='Bad token supersecrettokenvalue123 jData={"uid":"FA99999"}')])
    msg = db.get_results([rid])["error_message"].iloc[0]
    assert "supersecrettokenvalue123" not in msg and "FA99999" not in msg


def test_ws_samples_roundtrip(db):
    rid = make_run(db, run_type="websocket")
    db.insert_ws_samples(rid, [("inter_arrival_ms", 10.0, "t1", None), ("inter_arrival_ms", 12.0, "t2", "x")])
    df = db.get_ws_samples(rid)
    assert df["value"].tolist() == [10.0, 12.0]


def test_settings_roundtrip_and_secret_refusal(db, db_path):
    db.set_setting("default_iterations", 50)
    db.set_setting("ui", {"theme": "dark", "include_synthetic": False})
    assert Database(db_path).get_setting("default_iterations") == 50
    assert db.get_setting("ui")["include_synthetic"] is False
    assert db.get_setting("missing", "dflt") == "dflt"
    for bad_key in ("access_token", "TS_SECRET_KEY", "app_key", "my_password", "checksum", "client_id"):
        with pytest.raises(ValueError):
            db.set_setting(bad_key, "x")
    redaction.register_secrets(["registered-secret-xyz"])
    with pytest.raises(ValueError):
        db.set_setting("note", "registered-secret-xyz")
    with pytest.raises(ValueError):
        db.set_setting("nested", {"a": ["registered-secret-xyz"]})
    with pytest.raises(ValueError):
        db.set_setting("header", "Bearer abc.def")


def test_get_database_creates_dirs(tmp_path):
    from src.settings import load_settings

    settings = load_settings(env_file=tmp_path / "none.env")
    settings.db_path = tmp_path / "deep" / "dir" / "b.db"
    db = get_database(settings)
    assert settings.db_path.exists()
    assert isinstance(db.list_runs(), pd.DataFrame)
