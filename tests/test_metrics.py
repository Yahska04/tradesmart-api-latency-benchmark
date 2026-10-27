"""Tests for src.metrics: percentiles, summaries, comparisons."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src import metrics
from src.metrics import (
    METRIC_COLUMNS,
    comparable_conditions,
    compare_runs,
    endpoint_metrics,
    overall_metrics,
    percentile,
    safe_change,
    summarize,
)


def make_rows(latencies, *, endpoint_id="EP", success=True, is_warmup=False, is_timeout=False,
              outcome=None, start="2026-10-16T10:00:00.000000Z", step_s=1.0):
    """Build request-level rows; one row per latency, spaced ``step_s`` apart."""
    base = pd.Timestamp(start)
    rows = []
    for i, lat in enumerate(latencies):
        st = base + pd.Timedelta(seconds=i * step_s)
        ct = st + pd.Timedelta(milliseconds=lat if lat is not None and not (isinstance(lat, float) and math.isnan(lat)) else 0)
        rows.append({
            "run_id": "r1", "endpoint_id": endpoint_id, "endpoint_name": f"{endpoint_id} name",
            "method": "POST", "seq": i + 1, "is_warmup": is_warmup, "latency_ms": lat,
            "success": success, "is_timeout": is_timeout,
            "outcome": outcome or ("success" if success else ("timeout" if is_timeout else "http_error")),
            "started_at": st.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "completed_at": ct.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        })
    return rows


# ------------------------------------------------------------------ percentile
def test_percentile_known_dataset_1_to_100():
    values = list(range(1, 101))
    assert percentile(values, 50) == pytest.approx(50.5)
    assert percentile(values, 95) == pytest.approx(95.05)
    assert percentile(values, 99) == pytest.approx(99.01)


def test_percentile_matches_numpy_linear():
    rng = np.random.default_rng(1)
    values = rng.lognormal(5, 0.4, 537)
    for q in (50, 95, 99):
        assert percentile(values, q) == pytest.approx(np.percentile(values, q, method="linear"))


def test_percentile_small_known_dataset():
    assert percentile([10, 20, 30, 40], 50) == pytest.approx(25.0)
    assert percentile([42.0], 99) == pytest.approx(42.0)


def test_percentile_empty_and_nan():
    assert percentile([], 50) is None
    assert percentile([float("nan"), None], 95) is None
    assert percentile([1.0, float("nan"), 3.0], 50) == pytest.approx(2.0)


# ------------------------------------------------------------------- summarize
def test_summarize_empty_dataframe_returns_none_values():
    for df in (pd.DataFrame(), pd.DataFrame(columns=["latency_ms", "success", "is_warmup"]), None):
        s = summarize(df)
        assert s["total"] == 0 and s["samples"] == 0
        for key in ("p50_ms", "p95_ms", "p99_ms", "min_ms", "max_ms", "mean_ms", "std_ms",
                    "success_rate", "failure_rate", "rps"):
            assert s[key] is None
        assert s["p95_reliable"] is False and s["p99_reliable"] is False
        assert s["warnings"]


def test_summarize_basic_stats_1_to_100():
    df = pd.DataFrame(make_rows(list(range(1, 101))))
    s = summarize(df)
    assert s["samples"] == 100 and s["total"] == 100 and s["successful"] == 100
    assert s["p50_ms"] == pytest.approx(50.5)
    assert s["p95_ms"] == pytest.approx(95.05)
    assert s["p99_ms"] == pytest.approx(99.01)
    assert s["min_ms"] == 1 and s["max_ms"] == 100
    assert s["mean_ms"] == pytest.approx(50.5)
    assert s["std_ms"] == pytest.approx(np.std(np.arange(1, 101), ddof=1))
    assert s["p95_reliable"] and s["p99_reliable"]
    assert not any("P99" in w for w in s["warnings"])


def test_summarize_insufficient_samples_warnings():
    s = summarize(pd.DataFrame(make_rows([100.0] * 10)))
    assert s["samples"] == 10
    assert not s["p95_reliable"] and not s["p99_reliable"]
    assert any("P95 based on 10 samples" in w for w in s["warnings"])
    assert any("P99 based on 10 samples (< 100)" in w for w in s["warnings"])

    s = summarize(pd.DataFrame(make_rows([100.0] * 50)))
    assert s["p95_reliable"] and not s["p99_reliable"]
    assert not any(w.startswith("P95") for w in s["warnings"])
    assert any("P99 based on 50 samples" in w for w in s["warnings"])


def test_single_sample_std_is_none():
    s = summarize(pd.DataFrame(make_rows([123.0])))
    assert s["std_ms"] is None and s["p50_ms"] == 123.0


def test_failures_and_timeouts_excluded_from_percentiles_but_counted():
    ok = make_rows([100.0] * 18)
    fast_fail = make_rows([1.0, 2.0], success=False, outcome="http_error")
    timeouts = make_rows([10000.0, 10000.0], success=False, is_timeout=True)
    limited = make_rows([5.0], success=False, outcome="rate_limited")
    s = summarize(pd.DataFrame(ok + fast_fail + timeouts + limited))
    assert s["total"] == 23
    assert s["successful"] == 18 and s["samples"] == 18
    assert s["failed"] == 5
    assert s["timeouts"] == 2
    assert s["rate_limited"] == 1
    assert s["min_ms"] == 100.0 and s["max_ms"] == 100.0
    assert s["p99_ms"] == 100.0
    assert s["success_rate"] == pytest.approx(18 / 23)
    assert s["failure_rate"] == pytest.approx(5 / 23)
    assert any("5 failed" in w for w in s["warnings"])


def test_all_failures_gives_no_percentiles():
    s = summarize(pd.DataFrame(make_rows([50.0, 60.0], success=False)))
    assert s["total"] == 2 and s["successful"] == 0 and s["samples"] == 0
    assert s["p50_ms"] is None and s["success_rate"] == 0.0 and s["failure_rate"] == 1.0
    assert any("No successful" in w for w in s["warnings"])


def test_success_rate_math():
    rows = make_rows([10.0] * 97) + make_rows([10.0] * 3, success=False)
    s = summarize(pd.DataFrame(rows))
    assert s["success_rate"] == pytest.approx(0.97)
    assert s["failure_rate"] == pytest.approx(0.03)


def test_warmup_rows_excluded():
    rows = make_rows([5000.0, 4000.0], is_warmup=True) + make_rows([10.0, 20.0, 30.0])
    s = summarize(pd.DataFrame(rows))
    assert s["total"] == 3 and s["samples"] == 3
    assert s["max_ms"] == 30.0


def test_nan_latency_success_rows_skipped():
    rows = make_rows([10.0, float("nan"), 30.0])
    s = summarize(pd.DataFrame(rows))
    assert s["successful"] == 3 and s["samples"] == 2
    assert s["p50_ms"] == pytest.approx(20.0)
    assert any("no recorded latency" in w for w in s["warnings"])


def test_boolean_columns_accept_ints_and_strings():
    rows = make_rows([10.0, 20.0, 30.0])
    df = pd.DataFrame(rows)
    df["success"] = [1, "true", 0]
    df["is_warmup"] = [0, 0, "0"]
    s = summarize(df)
    assert s["total"] == 3 and s["successful"] == 2


def test_rps_from_wall_clock():
    # 11 requests started 1 s apart, each 0 ms long except last -> wall ~10 s
    s = summarize(pd.DataFrame(make_rows([0.0] * 11, step_s=1.0)))
    assert s["rps"] == pytest.approx(11 / 10.0)
    assert summarize(pd.DataFrame(make_rows([5.0])))["rps"] is None


# ------------------------------------------------------------ endpoint metrics
def test_endpoint_metrics_one_row_per_endpoint():
    df = pd.DataFrame(make_rows([1, 2, 3], endpoint_id="A") + make_rows([10, 20], endpoint_id="B")
                      + make_rows([999], endpoint_id="A", is_warmup=True))
    df["category"] = "Cat"
    em = endpoint_metrics(df)
    assert list(em.columns) == METRIC_COLUMNS
    assert set(em["endpoint_id"]) == {"A", "B"}
    a = em.set_index("endpoint_id").loc["A"]
    assert a["samples"] == 3 and a["max_ms"] == 3 and a["category"] == "Cat"
    assert a["endpoint_name"] == "A name" and a["method"] == "POST"
    assert isinstance(a["warnings"], list)


def test_endpoint_metrics_empty():
    em = endpoint_metrics(pd.DataFrame())
    assert list(em.columns) == METRIC_COLUMNS and len(em) == 0


def test_overall_metrics_counts_endpoints():
    df = pd.DataFrame(make_rows([1, 2], endpoint_id="A") + make_rows([3], endpoint_id="B"))
    o = overall_metrics(df)
    assert o["endpoints"] == 2 and o["total"] == 3
    assert overall_metrics(pd.DataFrame())["endpoints"] == 0


# ------------------------------------------------------------------ comparison
def test_safe_change():
    assert safe_change(100.0, 120.0) == (pytest.approx(20.0), pytest.approx(20.0))
    assert safe_change(200.0, 150.0) == (pytest.approx(-50.0), pytest.approx(-25.0))
    assert safe_change(0.0, 10.0) == (10.0, None)
    assert safe_change(None, 10.0) == (None, None)
    assert safe_change(10.0, None) == (None, None)
    assert safe_change(float("nan"), 5.0) == (None, None)


def test_compare_runs_with_zero_baseline_and_missing_endpoints():
    base = pd.DataFrame([
        {"endpoint_id": "A", "endpoint_name": "Alpha", "p50_ms": 100.0, "p95_ms": 0.0, "p99_ms": None,
         "success_rate": 1.0, "failure_rate": 0.0},
        {"endpoint_id": "B", "endpoint_name": "Beta", "p50_ms": 50.0, "p95_ms": 60.0, "p99_ms": 70.0,
         "success_rate": 0.9, "failure_rate": 0.1},
    ])
    cand = pd.DataFrame([
        {"endpoint_id": "A", "endpoint_name": "Alpha", "p50_ms": 150.0, "p95_ms": 10.0, "p99_ms": 200.0,
         "success_rate": 0.98, "failure_rate": 0.02},
        {"endpoint_id": "C", "endpoint_name": "Gamma", "p50_ms": 30.0, "p95_ms": 40.0, "p99_ms": 45.0,
         "success_rate": 1.0, "failure_rate": 0.0},
    ])
    cmp_ = compare_runs(base, cand).set_index("endpoint_id")
    assert cmp_.loc["A", "status"] == "both"
    assert cmp_.loc["B", "status"] == "baseline_only"
    assert cmp_.loc["C", "status"] == "candidate_only"
    assert cmp_.loc["A", "p50_abs"] == pytest.approx(50.0)
    assert cmp_.loc["A", "p50_pct"] == pytest.approx(50.0)
    assert cmp_.loc["A", "p95_abs"] == pytest.approx(10.0)
    assert pd.isna(cmp_.loc["A", "p95_pct"])  # zero baseline -> no percent
    assert pd.isna(cmp_.loc["A", "p99_abs"])  # missing baseline
    assert pd.isna(cmp_.loc["B", "p50_cand"]) and pd.isna(cmp_.loc["B", "p50_abs"])
    assert pd.isna(cmp_.loc["C", "p50_base"])
    assert cmp_.loc["C", "endpoint_name"] == "Gamma"
    assert cmp_.loc["A", "failure_rate_pct"] is None or pd.isna(cmp_.loc["A", "failure_rate_pct"])
    for col in ("success_rate_base", "success_rate_cand", "success_rate_abs", "success_rate_pct"):
        assert col in cmp_.columns


def test_compare_runs_empty_inputs():
    out = compare_runs(pd.DataFrame(), pd.DataFrame(columns=METRIC_COLUMNS))
    assert len(out) == 0 and "status" in out.columns and "p99_pct" in out.columns


def test_comparable_conditions():
    run_a = {"run_type": "http", "connection_mode": "warm", "environment": "live", "base_host": "h1",
             "is_synthetic": False,
             "config": {"iterations": 20, "delay_ms": 500, "timeout_s": 10.0, "concurrency": 1}}
    run_b = {"run_type": "http", "connection_mode": "cold", "environment": "live", "base_host": "h1",
             "is_synthetic": False,
             "config": {"iterations": 50, "delay_ms": 500, "timeout_s": 10, "concurrency": 1}}
    diffs = comparable_conditions(run_a, run_b)
    assert any("Connection mode" in d for d in diffs)
    assert any("Iterations" in d for d in diffs)
    assert not any("Timeout" in d for d in diffs)  # 10.0 == 10
    assert comparable_conditions(run_a, dict(run_a)) == []
    assert comparable_conditions(run_a, None)  # type: ignore[arg-type]
    run_c = dict(run_a, is_synthetic=True, run_type="demo")
    assert any("SYNTHETIC" in d for d in comparable_conditions(run_a, run_c))


def test_constants():
    assert metrics.PERCENTILE_METHOD == "linear"
    assert metrics.MIN_SAMPLES_P95 == 20 and metrics.MIN_SAMPLES_P99 == 100
