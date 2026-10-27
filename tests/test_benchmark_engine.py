"""Tests for src.benchmark_engine with a FakeDatabase, fake clock and httpx.MockTransport."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pandas as pd
import pytest

from src.api_client import RequestResult, TradeSmartClient
from src.benchmark_engine import BenchmarkConfig, BenchmarkRunner, build_plan, record_manual_result
from src.endpoint_registry import load_endpoints

FIXTURE = Path(__file__).parent / "fixtures" / "endpoints_sample.json"
BASE = "https://mock.tradesmart.test/NorenWClientAPIv2"
CTX = {"ClientId": "AB1234", "exch": "NSE", "token": "22"}


class FakeDatabase:
    """Records every call made by the runner."""

    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}
        self.results: list[dict] = []
        self.insert_batches: list[int] = []
        self.metrics: dict[str, pd.DataFrame] = {}
        self.finished: dict[str, tuple[str, str | None]] = {}

    def create_run(self, *, run_id, run_type, environment, base_host, connection_mode, config, planned_requests,
                   is_synthetic=False, notes=""):
        json.dumps(config)  # must be serialisable
        self.runs[run_id] = dict(run_type=run_type, environment=environment, base_host=base_host,
                                 connection_mode=connection_mode, config=config, planned_requests=planned_requests,
                                 notes=notes)
        return run_id

    def insert_results(self, results):
        rows = [r.to_row() if isinstance(r, RequestResult) else dict(r) for r in results]
        self.results.extend(rows)
        self.insert_batches.append(len(rows))
        return len(rows)

    def finish_run(self, run_id, status, stop_reason=None):
        self.finished[run_id] = (status, stop_reason)

    def save_endpoint_metrics(self, run_id, metrics):
        self.metrics[run_id] = metrics

    def recompute_metrics(self, run_id):
        df = pd.DataFrame([r for r in self.results if r["run_id"] == run_id])
        from src.metrics import endpoint_metrics

        m = endpoint_metrics(df)
        self.metrics[run_id] = m
        return m


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def endpoints():
    return load_endpoints(FIXTURE)


def ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith(("/PlaceOrder", "/CancelOrder", "/GenAcsTok")):
        pytest.fail(f"state-changing endpoint was sent: {request.url.path}")
    return httpx.Response(200, json={"stat": "Ok"})


def runner_for(endpoints, handler, clock: FakeClock | None = None, db: FakeDatabase | None = None):
    clock = clock or FakeClock()
    db = db or FakeDatabase()
    seen_configs = []

    def factory(cfg: BenchmarkConfig) -> TradeSmartClient:
        seen_configs.append(cfg)
        return TradeSmartClient(BASE, access_token="tok-secret-123", timeout_s=cfg.timeout_s,
                                connection_mode=cfg.connection_mode, transport=httpx.MockTransport(handler))

    runner = BenchmarkRunner(db, endpoints, factory, BASE, sleep=clock.sleep, clock=clock)
    return runner, db, clock, seen_configs


# --------------------------------------------------------------------------------------------------
# config / plan
# --------------------------------------------------------------------------------------------------
def test_config_validate():
    assert BenchmarkConfig(endpoint_ids=["UserDetails"]).validate() == []
    bad = BenchmarkConfig(endpoint_ids=[], iterations=0, warmup=50, connection_mode="hot", order="x",
                          concurrency=9, environment="prod", safety_factor=2)
    problems = " | ".join(bad.validate())
    for word in ("endpoint", "iterations", "warmup", "connection_mode", "order", "concurrency", "environment",
                 "safety_factor"):
        assert word in problems


def test_config_to_dict_masks_account_ids():
    cfg = BenchmarkConfig(endpoint_ids=["UserDetails"], payload_context={"ClientId": "AB123456", "uid": "AB123456",
                                                                         "actid": "AB123456", "exch": "NSE"},
                          payload_overrides={"Limits": {"actid": "AB123456"}})
    d = cfg.to_dict()
    text = json.dumps(d)
    assert "AB123456" not in text
    assert d["payload_context"]["exch"] == "NSE"
    assert cfg.payload_context["ClientId"] == "AB123456"  # original untouched


def test_build_plan_counts_blocked_and_missing(endpoints):
    cfg = BenchmarkConfig(endpoint_ids=["UserDetails", "GetQuotes", "PlaceOrder", "GenAcsTok", "Nope"],
                          iterations=10, warmup=2, delay_ms=100, payload_context={"ClientId": "AB1234"})
    plan = build_plan(cfg, endpoints, typical_latency_ms=100)
    blocked = {b["endpoint_id"]: b for b in plan.blocked}
    assert set(blocked) == {"GetQuotes", "PlaceOrder", "GenAcsTok", "Nope"}
    assert blocked["GetQuotes"]["missing"] == ["exch", "token"]
    assert "unsafe" in blocked["PlaceOrder"]["reason"]
    assert plan.runnable_ids == ["UserDetails"]
    assert (plan.total_requests, plan.warmup_requests, plan.measured_requests) == (12, 2, 10)
    assert plan.estimated_duration_s == pytest.approx(12 * 0.2)
    assert any("LIVE" in w for w in plan.warnings)


def test_build_plan_rate_limit_bound(endpoints):
    # 0 delay: duration bounded by the 5 req/s cap for undocumented endpoints
    cfg = BenchmarkConfig(endpoint_ids=["UserDetails"], iterations=100, warmup=0, delay_ms=0,
                          payload_context=CTX, environment="mock")
    plan = build_plan(cfg, endpoints, typical_latency_ms=10)
    assert plan.per_endpoint[0]["effective_rate_limit"] == 5
    assert plan.estimated_duration_s == pytest.approx(99 / 5)
    cfg.max_duration_s = 5
    plan = build_plan(cfg, endpoints, typical_latency_ms=10)
    assert plan.estimated_duration_s == 5 and any("max duration" in w for w in plan.warnings)


# --------------------------------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------------------------------
def test_runner_round_robin_warmups_and_metrics(endpoints):
    runner, db, clock, seen = runner_for(endpoints, ok_handler)
    cfg = BenchmarkConfig(endpoint_ids=["UserDetails", "GetQuotes"], iterations=3, warmup=1, delay_ms=50,
                          payload_context=CTX)
    progress = []
    run_id = runner.run(cfg, progress_cb=progress.append)
    assert db.runs[run_id]["run_type"] == "http"
    assert db.runs[run_id]["planned_requests"] == 8
    assert db.runs[run_id]["base_host"] == "mock.tradesmart.test"
    assert "AB1234" not in json.dumps(db.runs[run_id]["config"])
    order = [(r["endpoint_id"], r["is_warmup"]) for r in db.results]
    assert order == [("UserDetails", True), ("GetQuotes", True)] + [("UserDetails", False), ("GetQuotes", False)] * 3
    assert all(r["success"] and r["latency_ms"] is not None for r in db.results)
    assert db.finished[run_id] == ("completed", None)
    m = db.metrics[run_id]
    assert set(m["endpoint_id"]) == {"UserDetails", "GetQuotes"}
    assert m.set_index("endpoint_id").loc["UserDetails", "samples"] == 3  # warm-ups excluded
    assert len(progress) == 8 and progress[-1].completed == 8 and progress[-1].total == 8
    assert clock.sleeps.count(0.05) == 7  # delay between consecutive requests only
    assert seen[0].connection_mode == "warm"


def test_runner_grouped_order(endpoints):
    runner, db, *_ = runner_for(endpoints, ok_handler)
    cfg = BenchmarkConfig(endpoint_ids=["UserDetails", "Limits"], iterations=2, warmup=0, delay_ms=0,
                          order="grouped", payload_context=CTX)
    runner.run(cfg)
    assert [r["endpoint_id"] for r in db.results] == ["UserDetails"] * 2 + ["Limits"] * 2
    assert [r["seq"] for r in db.results] == [1, 2, 1, 2]


def test_runner_never_sends_state_changing(endpoints):
    """PlaceOrder/CancelOrder/GenAcsTok must never reach the transport, even in mock with no opt-in."""
    hits = []

    def handler(request):
        hits.append(request.url.path)
        if not request.url.path.endswith("/UserDetails"):
            pytest.fail(f"blocked endpoint was requested: {request.url.path}")
        return httpx.Response(200, json={"stat": "Ok"})

    for env in ("live", "mock"):
        runner, db, *_ = runner_for(endpoints, handler)
        cfg = BenchmarkConfig(endpoint_ids=["PlaceOrder", "UserDetails", "CancelOrder", "GenAcsTok"], iterations=2,
                              warmup=0, delay_ms=0, environment=env, payload_context=CTX,
                              state_changing_test_mode=(env == "live"), user_confirmed=(env == "live"))
        run_id = runner.run(cfg)
        status, reason = db.finished[run_id]
        assert status == "completed"
        assert "PlaceOrder" in reason and "CancelOrder" in reason and "blocked" in reason
        assert "PlaceOrder" in db.runs[run_id]["notes"]
        assert {r["endpoint_id"] for r in db.results} == {"UserDetails"}
    assert all(p.endswith("/UserDetails") for p in hits)


def test_runner_only_blocked_endpoints_fails_without_requests(endpoints):
    runner, db, *_ = runner_for(endpoints, lambda r: pytest.fail("no request expected"))
    run_id = runner.run(BenchmarkConfig(endpoint_ids=["PlaceOrder"], payload_context=CTX))
    assert db.finished[run_id][0] == "failed" and "no runnable" in db.finished[run_id][1]
    assert db.results == []


def test_runner_failures_recorded_and_circuit_breaker(endpoints):
    def handler(request):
        if request.url.path.endswith("/Limits"):
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json={"stat": "Ok"})

    runner, db, *_ = runner_for(endpoints, handler)
    cfg = BenchmarkConfig(endpoint_ids=["UserDetails", "Limits"], iterations=10, warmup=0, delay_ms=0,
                          max_consecutive_failures=3, payload_context=CTX)
    run_id = runner.run(cfg)
    limits = [r for r in db.results if r["endpoint_id"] == "Limits"]
    assert len(limits) == 3 and all(r["outcome"] == "timeout" and r["is_timeout"] for r in limits)
    assert all(r["latency_ms"] is not None for r in limits)  # failures keep real elapsed time
    assert len([r for r in db.results if r["endpoint_id"] == "UserDetails"]) == 10
    status, reason = db.finished[run_id]
    assert status == "completed" and "paused: Limits" in reason


def test_runner_global_stop_on_rate_limit(endpoints):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, text="too many") if len(calls) == 3 else httpx.Response(200, json={"stat": "Ok"})

    runner, db, *_ = runner_for(endpoints, handler)
    cfg = BenchmarkConfig(endpoint_ids=["UserDetails", "Limits"], iterations=10, warmup=0, delay_ms=0,
                          payload_context=CTX)
    run_id = runner.run(cfg)
    assert len(calls) == 3
    status, reason = db.finished[run_id]
    assert status == "stopped" and "429" in reason
    assert db.results[-1]["outcome"] == "rate_limited"


def test_runner_stop_event(endpoints):
    stop = threading.Event()
    runner, db, *_ = runner_for(endpoints, ok_handler)
    cfg = BenchmarkConfig(endpoint_ids=["UserDetails"], iterations=50, warmup=0, delay_ms=0, payload_context=CTX)

    def progress(p):
        if p.completed == 4:
            stop.set()

    run_id = runner.run(cfg, progress_cb=progress, stop_event=stop)
    assert len(db.results) == 4
    assert db.finished[run_id] == ("stopped", "stopped by user")
    assert sum(db.insert_batches) == 4


def test_runner_max_duration(endpoints):
    runner, db, clock, _ = runner_for(endpoints, ok_handler)
    cfg = BenchmarkConfig(endpoint_ids=["UserDetails"], iterations=100, warmup=0, delay_ms=1000, max_duration_s=5,
                          payload_context=CTX)
    run_id = runner.run(cfg)
    status, reason = db.finished[run_id]
    assert status == "stopped" and "max duration" in reason
    assert 5 <= len(db.results) <= 7


def test_runner_rate_limiter_paces_with_fake_clock(endpoints):
    runner, db, clock, _ = runner_for(endpoints, ok_handler)
    cfg = BenchmarkConfig(endpoint_ids=["UserDetails"], iterations=11, warmup=0, delay_ms=0, payload_context=CTX)
    runner.run(cfg)
    assert len(db.results) == 11
    assert clock.now == pytest.approx(2.0)  # 5/s cap for undocumented endpoints -> two window slides


def test_runner_batches_flush_every_25(endpoints):
    runner, db, *_ = runner_for(endpoints, ok_handler)
    cfg = BenchmarkConfig(endpoint_ids=["UserDetails", "Limits"], iterations=30, warmup=0, delay_ms=0,
                          payload_context=CTX)
    runner.run(cfg)
    assert db.insert_batches == [25, 25, 10]


def test_runner_cold_mode_and_concurrency(endpoints):
    lock = threading.Lock()
    paths = []

    def handler(request):
        with lock:
            paths.append(request.url.path)
        return httpx.Response(200, json={"stat": "Ok"})

    runner, db, _, seen = runner_for(endpoints, handler)
    cfg = BenchmarkConfig(endpoint_ids=["UserDetails", "Limits", "OrderBook"], iterations=4, warmup=1, delay_ms=0,
                          concurrency=3, connection_mode="cold", payload_context=CTX)
    run_id = runner.run(cfg)
    assert len(db.results) == 15 and len(paths) == 15
    assert all(r["connection_mode"] == "cold" for r in db.results)
    assert db.finished[run_id][0] == "completed"


def test_runner_invalid_config_raises(endpoints):
    runner, *_ = runner_for(endpoints, ok_handler)
    with pytest.raises(ValueError):
        runner.run(BenchmarkConfig(endpoint_ids=["UserDetails"], iterations=0))


def test_record_manual_result(endpoints):
    db = FakeDatabase()
    place = next(e for e in endpoints if e.id == "PlaceOrder")
    run_id = record_manual_result(db, place, 123.4, 200, True, "live", notes="measured in broker terminal")
    assert db.runs[run_id]["run_type"] == "manual"
    row = db.results[0]
    assert row["source"] == "manual" and row["latency_ms"] == 123.4 and row["outcome"] == "success"
    assert db.finished[run_id][0] == "completed"
    run_id2 = record_manual_result(db, place, 99.0, 500, False, "live", run_id=run_id)
    assert run_id2 == run_id and db.results[-1]["outcome"] == "http_error"
    with pytest.raises(ValueError):
        record_manual_result(db, place, -1, 200, True, "live")
