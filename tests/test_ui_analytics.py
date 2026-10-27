"""Run-comparison helpers (src/ui/analytics.py) and comparison charts (src/ui/components.py)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from src.metrics import compare_runs
from src.ui import analytics as A
from src.ui import components as C


def _run(run_id, started, *, mode="warm", run_type="http", synthetic=False, source=None,
         iterations=20, delay_ms=500, order="round_robin", warmup=2):
    cfg = {"iterations": iterations, "warmup": warmup, "delay_ms": delay_ms, "order": order,
           "timeout_s": 10.0, "concurrency": 1, "connection_mode": mode}
    if source:
        cfg["source"] = source
    return {"run_id": run_id, "run_type": run_type, "started_at": started, "status": "completed",
            "is_synthetic": synthetic, "environment": "demo" if synthetic else "live",
            "base_host": "synthetic.demo" if synthetic else "api.example", "connection_mode": mode,
            "config_json": json.dumps(cfg)}


# Newest first, like Database.list_runs: the user's real mix of warm/cold/order-test runs.
WARM_NEW = _run("run-20261019-083029-efb72d", "2026-10-19T08:30:29Z")
ORDER_B = _run("order-20261019-082553-3e5045", "2026-10-19T08:25:53Z", source="order_test",
               iterations=7, delay_ms=2000, order="grouped", warmup=0)
ORDER_A = _run("order-20261019-082053-74a21c", "2026-10-19T08:20:53Z", source="order_test",
               iterations=3, delay_ms=2000, order="grouped", warmup=0)
COLD = _run("run-20261019-081432-94c69e", "2026-10-19T08:14:32Z", mode="cold")
WARM_OLD = _run("run-20261019-072434-e1e6b9", "2026-10-19T07:24:34Z")
RUNS = [WARM_NEW, ORDER_B, ORDER_A, COLD, WARM_OLD]
READ = {"UserDetails", "Limits", "Holdings", "OrderBook", "TradeBook", "PositionBook"}
EPS = {WARM_NEW["run_id"]: READ | {"GetOrderMargin"}, ORDER_B["run_id"]: {"PlaceOrder", "CancelOrder"},
       ORDER_A["run_id"]: {"PlaceOrder", "CancelOrder"}, COLD["run_id"]: set(READ),
       WARM_OLD["run_id"]: set(READ)}


def test_run_kind_and_short_label():
    assert A.run_kind(ORDER_B) == A.ORDER_TEST
    assert A.run_kind(WARM_NEW) == "Benchmark"
    assert A.run_kind(_run("x", "2026-10-19T08:00:00Z", source="endpoint_check")) == A.ENDPOINT_CHECK
    assert A.run_kind({"run_id": "d", "run_type": "demo", "config": {}}) == "Synthetic demo"
    label = A.short_run_label(COLD)
    assert label == "19 Oct 13:44 · cold · 94c69e"            # IST, connection mode, id suffix
    assert "order latency test" in A.short_run_label(ORDER_B)
    assert "SYNTHETIC" in A.short_run_label(_run("demo-1", "2026-10-13T05:09:33Z", synthetic=True))


def test_run_config_reads_parsed_and_json_forms():
    assert A.run_config({"config": {"a": 1}}) == {"a": 1}
    assert A.run_config({"config_json": '{"b": 2}'}) == {"b": 2}
    assert A.run_config({"config_json": "not json"}) == {}
    assert A.run_config(None) == {}


def test_default_pair_skips_order_tests_and_prefers_matching_conditions():
    base, cand = A.default_run_pair(RUNS, EPS)
    # Latest benchmark vs the previous warm benchmark (not the newer order test, not the cold run).
    assert (base, cand) == (WARM_OLD["run_id"], WARM_NEW["run_id"])


def test_default_pair_falls_back_when_no_identical_conditions():
    runs = [WARM_NEW, ORDER_B, COLD]
    base, cand = A.default_run_pair(runs, EPS)
    assert (base, cand) == (COLD["run_id"], WARM_NEW["run_id"])   # shares endpoints
    # Nothing shares endpoints: still returns two different runs.
    base, cand = A.default_run_pair([WARM_NEW, ORDER_B], EPS)
    assert (base, cand) == (ORDER_B["run_id"], WARM_NEW["run_id"])


def test_default_pair_edge_cases():
    assert A.default_run_pair([], {}) == (None, None)
    assert A.default_run_pair([WARM_NEW], EPS) == (None, WARM_NEW["run_id"])
    synth = [_run("demo-2", "2026-10-15T04:38:27Z", synthetic=True, run_type="demo"),
             _run("demo-1", "2026-10-13T05:09:33Z", synthetic=True, run_type="demo")]
    assert A.default_run_pair(synth, {"demo-2": {"A"}, "demo-1": {"A"}}) == ("demo-1", "demo-2")


def test_condition_table_and_comparability():
    df, differing = A.condition_table([COLD, WARM_NEW])
    assert differing == ["Connection"]
    assert list(df["Connection"]) == ["cold", "warm"]
    assert not A.runs_comparable([COLD, WARM_NEW])
    assert A.runs_comparable([WARM_OLD, WARM_NEW])
    assert A.runs_comparable([WARM_NEW])
    _, differing = A.condition_table([WARM_NEW, ORDER_B])
    assert {"Run kind", "Iterations", "Warm-up", "Delay (ms)", "Request order"} <= set(differing)


def _metrics(rows):
    return pd.DataFrame(rows, columns=["endpoint_id", "endpoint_name", "samples", "p50_ms", "p95_ms",
                                       "p99_ms", "success_rate", "failure_rate"])


def test_run_change_frame_handles_zero_baseline_and_one_sided_endpoints():
    base = _metrics([["A", "Alpha", 20, 10.0, 20.0, 0.0, 1.0, 0.0],
                     ["B", "Beta", 20, 30.0, 40.0, 50.0, 0.9, 0.1],
                     ["OLD", "Old", 5, 1.0, 1.0, 1.0, 1.0, 0.0]])
    cand = _metrics([["A", "Alpha", 18, 12.0, 15.0, 5.0, 0.9, 0.1],
                     ["B", "Beta", 20, None, None, None, 0.0, 1.0],
                     ["NEW", "New", 5, 1.0, 1.0, 1.0, 1.0, 0.0]])
    cmp = compare_runs(base, cand)
    ch = A.run_change_frame(cmp, base, cand).set_index("endpoint_id")
    assert list(ch.index) == ["A", "B"]                     # one-sided endpoints excluded
    assert ch.loc["A", "p50_abs"] == pytest.approx(2.0)
    assert ch.loc["A", "p50_pct"] == pytest.approx(20.0)
    assert ch.loc["A", "p95_abs"] == pytest.approx(-5.0)
    assert ch.loc["A", "p99_abs"] == pytest.approx(5.0)
    assert pd.isna(ch.loc["A", "p99_pct"])                  # zero baseline -> no % change
    assert pd.isna(ch.loc["B", "p50_abs"])                  # missing candidate -> no change
    assert ch.loc["A", "success_pts"] == pytest.approx(-10.0)
    assert ch.loc["B", "failure_pts"] == pytest.approx(90.0)
    assert (ch.loc["A", "samples_base"], ch.loc["A", "samples_cand"]) == (20, 18)
    counts = A.change_counts(ch.reset_index(), "p95")
    assert counts == {"faster": 1, "slower": 0, "unchanged": 0, "measured": 1,
                      "median_pct": pytest.approx(-25.0)}


def test_run_change_frame_empty_inputs():
    assert not len(A.run_change_frame(pd.DataFrame(), pd.DataFrame(), pd.DataFrame()))
    only = compare_runs(_metrics([["A", "Alpha", 1, 1, 1, 1, 1, 0]]), _metrics([]))
    assert not len(A.run_change_frame(only, pd.DataFrame(), pd.DataFrame()))
    assert A.change_counts(A.run_change_frame(only, pd.DataFrame(), pd.DataFrame()), "p50")[
        "median_pct"] is None


def test_endpoint_labels_fall_back_and_disambiguate():
    m = pd.DataFrame({"endpoint_id": ["A", "B", "C", "A"], "endpoint_name": ["Same", "Same", None, None]})
    assert A.endpoint_labels(m) == {"A": "Same (A)", "B": "Same (B)", "C": "C"}


def test_style_change_table_formats_and_colours():
    df = pd.DataFrame({"P50 Δ ms": [-1.25, 2.0, None], "P50 Δ %": [-10.0, None, 0.0],
                       "Success Δ": [5.0, -5.0, 0.0]})
    sty = C.style_change_table(df, lower_is_better=["P50 Δ ms", "P50 Δ %"],
                               higher_is_better=["Success Δ"], ms_cols=["P50 Δ ms"],
                               pct_cols=["P50 Δ %"], pts_cols=["Success Δ"])
    html = sty.to_html()
    assert "▼ -1.2 ms" in html and "▲ +2.0 ms" in html and "—" in html
    assert "n/a" in html and "▲ +5.0 pts" in html
    ctx = sty._compute().ctx
    green, red = "#007A60", "#B01E45"
    assert any(green in v for k, styles in ctx.items() if k == (0, 0) for _, v in styles)
    assert any(red in v for k, styles in ctx.items() if k == (1, 0) for _, v in styles)
    assert any(red in v for k, styles in ctx.items() if k == (1, 2) for _, v in styles)


def _comp():
    return pd.DataFrame({
        "run_id": ["r1", "r1", "r2"], "endpoint_id": ["A", "B", "A"],
        "endpoint_name": ["Alpha", "Beta", "Alpha"], "samples": [20, 20, 20],
        "p50_ms": [10.0, 20.0, 12.0], "p95_ms": [15.0, 25.0, 18.0], "p99_ms": [20.0, 30.0, 25.0]})


def test_comparison_charts_keep_runs_separate():
    comp = _comp()
    fig = C.run_bars(comp, "p50_ms", ["r1", "r2"], {"r1": "Run one", "r2": "Run two"}, ["Alpha", "Beta"])
    assert [t.name for t in fig.data] == ["Run two", "Run one"]       # one trace per run, never pooled
    r2 = fig.data[0]
    assert list(r2.y) == ["Beta", "Alpha"] and pd.isna(r2.x[0]) and r2.x[1] == 12.0
    panels = C.comparison_bars_by_run(comp, ["r1", "r2"], {"r1": "Run one", "r2": "Run two"},
                                      ["Alpha", "Beta"])
    assert len(panels.data) == 6 and sum(bool(t.showlegend) for t in panels.data) == 3
    single = C.comparison_bars(comp[comp["run_id"] == "r1"], label_col="endpoint_name")
    assert [t.name for t in single.data] == ["P99", "P95", "P50"]


def test_change_dumbbell_and_trend_dashes():
    ch = pd.DataFrame({"endpoint_name": ["Alpha", "Beta"], "p50_base": [10.0, 5.0],
                       "p50_cand": [12.0, None], "p50_abs": [2.0, None], "p50_pct": [20.0, None]})
    fig = C.change_dumbbell(ch, "p50", "Baseline", "Candidate")
    assert [t.name for t in fig.data[1:]] == ["Baseline", "Candidate"]
    assert [a.text for a in fig.layout.annotations] == ["▲ +2.0 ms (+20%)"]
    hist = pd.DataFrame({"series": ["A · warm", "A · cold"], "ms": [10.0, 30.0], "run_id": ["r1", "r2"],
                         "run_time": pd.to_datetime(["2026-10-19T07:24:34Z", "2026-10-19T08:14:32Z"],
                                                    utc=True), "is_synthetic": [False, False]})
    tf = C.trend_lines(hist, "series", "ms", dashes={"A · cold": "dot"})
    assert {t.name: t.line.dash for t in tf.data} == {"A · warm": "solid", "A · cold": "dot"}
    assert str(tf.data[0].x[0]).startswith("2026-10-19T12:54")    # shown in IST
