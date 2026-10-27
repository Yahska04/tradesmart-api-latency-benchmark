"""Building blocks for the Performance analytics page."""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from src.ui import components as C
from src.ui import theme as T

HEALTH_FILTERS = {
    "all": "Any status",
    "clean": "All requests succeeded",
    "failures": "Has failures",
    "timeouts": "Has timeouts",
    "no_samples": "No successful samples",
}


def per_run_endpoint_metrics(results: pd.DataFrame, runs_by_id: dict[str, dict],
                             endpoints: list) -> pd.DataFrame:
    """Endpoint metrics computed per run (never pooled across runs)."""
    from src.metrics import endpoint_metrics

    frames = []
    if not len(results):
        return pd.DataFrame()
    for rid, g in results.groupby("run_id", sort=False):
        m = endpoint_metrics(g)
        if not len(m):
            continue
        run = runs_by_id.get(rid, {"run_id": rid})
        m.insert(0, "run_id", rid)
        m["run_label"] = C.run_label(run)
        m["is_synthetic"] = bool(run.get("is_synthetic"))
        frames.append(m)
    if not frames:
        return pd.DataFrame()
    return C.add_categories(pd.concat(frames, ignore_index=True), endpoints)


def apply_health_filter(df: pd.DataFrame, health: str) -> pd.DataFrame:
    if not len(df) or health == "all":
        return df
    failed = pd.to_numeric(df["failed"], errors="coerce").fillna(0)
    timeouts = pd.to_numeric(df["timeouts"], errors="coerce").fillna(0)
    samples = pd.to_numeric(df["samples"], errors="coerce").fillna(0)
    mask = {"clean": failed == 0, "failures": failed > 0, "timeouts": timeouts > 0,
            "no_samples": samples == 0}[health]
    return df[mask]


def percentile_cards(m: dict) -> None:
    samples = m.get("samples") or 0
    p95_ok = bool(m.get("p95_reliable", samples >= 20))
    p99_ok = bool(m.get("p99_reliable", samples >= 100))
    C.card_row([
        dict(label="Samples", value=C.fmt_int(samples), sub="Successful, non-warm-up",
             tone="accent"),
        dict(label="P50", value=C.fmt_ms(m.get("p50_ms")), unit="ms",
             sub=f"n = {C.fmt_int(samples)}"),
        dict(label="P95", value=C.fmt_ms(m.get("p95_ms")), unit="ms",
             sub=f"n = {C.fmt_int(samples)}" + ("" if p95_ok else " · indicative (< 20)"),
             sub_warn=not p95_ok),
        dict(label="P99", value=C.fmt_ms(m.get("p99_ms")), unit="ms",
             sub=f"n = {C.fmt_int(samples)}" + ("" if p99_ok else " · indicative (< 100)"),
             sub_warn=not p99_ok),
    ])
    st.write("")
    C.card_row([
        dict(label="Mean ± std", value=C.fmt_ms(m.get("mean_ms")), unit="ms",
             sub=f"std {C.fmt_ms(m.get('std_ms'))} ms"),
        dict(label="Min / max", value=f"{C.fmt_ms(m.get('min_ms'))} / {C.fmt_ms(m.get('max_ms'))}",
             unit="ms"),
        dict(label="Success rate", value=C.fmt_pct(m.get("success_rate")),
             sub=f"{C.fmt_int(m.get('successful'))} of {C.fmt_int(m.get('total'))}", tone="success"),
        dict(label="Failures / timeouts",
             value=f"{C.fmt_int(m.get('failed'))} / {C.fmt_int(m.get('timeouts'))}",
             sub=f"{C.fmt_int(m.get('rate_limited'))} rate-limited",
             tone="failure" if (m.get("failed") or 0) else "neutral"),
    ])


def render_endpoint_detail(db: Any, results: pd.DataFrame, run_id: str, endpoint_id: str,
                           key: str) -> None:
    from src.metrics import summarize

    rows = results[(results["run_id"] == run_id) & (results["endpoint_id"] == endpoint_id)]
    run = db.get_run(run_id) or {"run_id": run_id}
    if not len(rows):
        C.empty_state("No requests for this endpoint in this run",
                      "Pick another run or endpoint above.")
        return
    name = rows["endpoint_name"].dropna().iloc[0] if rows["endpoint_name"].notna().any() else endpoint_id
    method = rows["method"].dropna().iloc[0] if "method" in rows and rows["method"].notna().any() else ""
    badge = C.synthetic_badge(render=False) if run.get("is_synthetic") else ""
    st.markdown(f"### {C.esc(name)} &nbsp; {C.pill(method or 'POST', 'info')} {badge}",
                unsafe_allow_html=True)
    C.run_meta_block(run, request_count=len(rows))
    st.write("")
    m = summarize(rows)
    percentile_cards(m)
    C.show_sample_note(m.get("samples"))

    ok = rows[rows["success"]]
    lat = ok["latency_ms"].dropna().tolist()
    st.markdown("#### Latency distribution")
    if lat:
        C.show_chart(C.latency_histogram(lat, m), key=f"{key}_hist")
        C.show_chart(C.latency_box(lat, m, title="Box plot (quartiles: linear method)"),
                     key=f"{key}_box")
        st.caption("Histogram and box plot use successful requests only. Vertical lines mark P50, "
                   "P95 and P99 (numpy linear method).")
    else:
        st.info("No successful requests, so there is no latency distribution to show. See the "
                "status breakdown below.", icon=":material/info:")
    st.markdown("#### Latency over time")
    C.show_chart(C.latency_over_time(rows), key=f"{key}_time")
    st.caption("Every measured request. Failures (red ×) and timeouts (amber ▲) stay on the chart; "
               "failures with no response sit on the zero line.")

    s1, s2 = st.columns([1, 1.3])
    with s1:
        st.markdown("#### Status breakdown")
        C.show_chart(C.status_breakdown(rows), key=f"{key}_status")
    with s2:
        st.markdown("#### Failure and timeout history")
        bad = rows[~rows["success"]]
        if not len(bad):
            st.success("No failures or timeouts in this run.", icon=":material/check_circle:")
        else:
            hist = pd.DataFrame({
                "Time": [C.fmt_ts(x) for x in bad["started_at"]],
                "Outcome": bad["outcome"], "HTTP": bad["http_status"],
                "Timeout": bad["is_timeout"], "Latency (ms)": bad["latency_ms"],
                "API stat": bad.get("api_stat"), "Error": bad.get("error_message"),
            })
            st.dataframe(hist, hide_index=True, height=min(400, 38 + 35 * len(hist)),
                         column_config={"Latency (ms)": st.column_config.NumberColumn(format="%.1f")})


def conditions_differences(db: Any, run_ids: list[str]) -> list[str]:
    from src.metrics import comparable_conditions

    run_ids = list(dict.fromkeys(run_ids))
    if len(run_ids) < 2:
        return []
    base = db.get_run(run_ids[0])
    diffs: list[str] = []
    for other in run_ids[1:]:
        for d in comparable_conditions(base, db.get_run(other)):
            diffs.append(f"{run_ids[0]} vs {other}: {d}")
    return diffs


# ============================================================================ run comparison helpers
# Pure functions (no Streamlit calls) shared by the "Compare endpoints" view and the
# Historical comparison page. Runs are dicts from ``Database.get_run`` or ``list_runs`` records.
ORDER_TEST = "Order latency test"
ENDPOINT_CHECK = "Endpoint check"
_SPOT_CHECKS = (ORDER_TEST, ENDPOINT_CHECK)     # small ad-hoc runs, never the default candidate
_RUN_KINDS = {"http": "Benchmark", "manual": ENDPOINT_CHECK, "demo": "Synthetic demo",
              "websocket": "WebSocket"}
CONDITION_COLUMNS: list[tuple[str, str]] = [
    ("kind", "Run kind"), ("connection_mode", "Connection"), ("environment", "Environment"),
    ("iterations", "Iterations"), ("warmup", "Warm-up"), ("delay_ms", "Delay (ms)"),
    ("timeout_s", "Timeout (s)"), ("concurrency", "Concurrency"), ("order", "Request order"),
    ("is_synthetic", "Data"),
]


def run_config(run: dict | None) -> dict:
    """The run's parsed config (``get_run`` gives ``config``; ``list_runs`` rows carry JSON)."""
    import json

    if not run:
        return {}
    cfg = run.get("config")
    if isinstance(cfg, dict):
        return cfg
    raw = run.get("config_json")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def with_config(run: dict) -> dict:
    """Copy of ``run`` guaranteed to carry a parsed ``config`` (for ``comparable_conditions``)."""
    out = dict(run)
    out["config"] = run_config(run)
    return out


def run_kind(run: dict) -> str:
    source = run_config(run).get("source")
    if source == "order_test":
        return ORDER_TEST
    if source == "endpoint_check":
        return ENDPOINT_CHECK
    rt = str(run.get("run_type") or "")
    return _RUN_KINDS.get(rt, rt or "Run")


def run_connection(run: dict) -> str:
    return str(run.get("connection_mode") or run_config(run).get("connection_mode") or "")


def short_run_label(run: dict) -> str:
    """Compact, unique run name for legends and table cells: '19 Oct 14:00 · warm · efb72d'."""
    ts = C.parse_ts(run.get("started_at") or run.get("created_at"))
    when = ts.astimezone(C.IST).strftime("%d %b %H:%M") if ts else C.DASH
    parts = [when]
    if run_connection(run):
        parts.append(run_connection(run))
    if run_kind(run) in _SPOT_CHECKS:
        parts.append(run_kind(run).lower())
    if bool(run.get("is_synthetic")):
        parts.append("SYNTHETIC")
    parts.append(str(run.get("run_id", "?"))[-6:])
    return " · ".join(parts)


def run_conditions(run: dict) -> dict:
    """Recorded test conditions of one run, keyed like :data:`CONDITION_COLUMNS`."""
    cfg = run_config(run)

    def val(key: str) -> Any:
        v = run.get(key)
        if C._missing(v) or v == "":
            v = cfg.get(key)
        return None if C._missing(v) else v

    out = {key: val(key) for key, _ in CONDITION_COLUMNS}
    out["kind"] = run_kind(run)
    out["connection_mode"] = run_connection(run) or None
    out["is_synthetic"] = "SYNTHETIC" if bool(run.get("is_synthetic")) else "Measured"
    return out


def condition_table(runs: list[dict]) -> tuple[pd.DataFrame, list[str]]:
    """One row per run with its conditions, plus the labels of conditions that differ."""
    rows = []
    for run in runs:
        cond = run_conditions(run)
        row = {"Run": short_run_label(run)}
        row.update({label: ("—" if cond[key] is None else str(cond[key]))
                    for key, label in CONDITION_COLUMNS})
        rows.append(row)
    df = pd.DataFrame(rows)
    differing = [label for _, label in CONDITION_COLUMNS
                 if len(df) > 1 and df[label].nunique(dropna=False) > 1]
    return df, differing


def runs_comparable(runs: list[dict]) -> bool:
    """True when every run shares the first run's recorded conditions."""
    from src.metrics import comparable_conditions

    if len(runs) < 2:
        return True
    first = with_config(runs[0])
    return all(not comparable_conditions(first, with_config(r)) for r in runs[1:])


def default_run_pair(runs: list[dict], endpoints_by_run: dict[str, set]) -> tuple[str | None, str | None]:
    """Default (baseline, candidate) for a two-run comparison.

    ``runs`` are newest first. Candidate = latest measured benchmark run (order-latency tests,
    endpoint checks and synthetic runs only when nothing else exists). Baseline = the most recent earlier run that
    shares endpoints with it, preferring one recorded under identical conditions, then the same
    run kind and connection mode, then any run with shared endpoints, then simply the previous run.
    """
    from src.metrics import comparable_conditions

    if not runs:
        return None, None
    ids = [str(r["run_id"]) for r in runs]
    if len(runs) == 1:
        return None, ids[0]
    measured = [r for r in runs if not bool(r.get("is_synthetic"))] or runs
    preferred = [r for r in measured if run_kind(r) not in _SPOT_CHECKS] or measured
    cand = preferred[0]
    cand_id = str(cand["run_id"])
    cand_eps = endpoints_by_run.get(cand_id, set())
    older = runs[ids.index(cand_id) + 1:]
    others = older or [r for r in runs if str(r["run_id"]) != cand_id]
    sharing = [r for r in others if endpoints_by_run.get(str(r["run_id"]), set()) & cand_eps]
    tiers = [
        [r for r in sharing if not comparable_conditions(with_config(r), with_config(cand))],
        [r for r in sharing if run_kind(r) == run_kind(cand)
         and run_connection(r) == run_connection(cand)
         and bool(r.get("is_synthetic")) == bool(cand.get("is_synthetic"))],
        sharing,
        others,
    ]
    for tier in tiers:
        if tier:
            return str(tier[0]["run_id"]), cand_id
    return None, cand_id


def run_change_frame(cmp: pd.DataFrame, base_m: pd.DataFrame, cand_m: pd.DataFrame) -> pd.DataFrame:
    """Numeric change table for endpoints measured in both runs (from ``metrics.compare_runs``).

    Latency columns are ms; ``*_pct`` are percent (None when the baseline is zero/missing);
    rate columns are percentages (0..100) and ``*_pts`` their change in percentage points.
    """
    cols = ["endpoint_id", "endpoint_name", "samples_base", "samples_cand"]
    for p in ("p50", "p95", "p99"):
        cols += [f"{p}_base", f"{p}_cand", f"{p}_abs", f"{p}_pct"]
    cols += ["success_base", "success_cand", "success_pts", "failure_base", "failure_cand",
             "failure_pts"]
    if cmp is None or not len(cmp):
        return pd.DataFrame(columns=cols)
    both = cmp[cmp["status"] == "both"].copy()
    if not len(both):
        return pd.DataFrame(columns=cols)

    def samples(frame: pd.DataFrame) -> dict:
        if frame is None or not len(frame) or "samples" not in frame.columns:
            return {}
        return dict(zip(frame["endpoint_id"].astype(str), frame["samples"]))

    sb, sc = samples(base_m), samples(cand_m)
    out = pd.DataFrame({"endpoint_id": both["endpoint_id"].astype(str),
                        "endpoint_name": both["endpoint_name"].fillna(both["endpoint_id"])})
    out["samples_base"] = out["endpoint_id"].map(sb)
    out["samples_cand"] = out["endpoint_id"].map(sc)
    for p in ("p50", "p95", "p99"):
        for suffix in ("base", "cand", "abs", "pct"):
            out[f"{p}_{suffix}"] = pd.to_numeric(both[f"{p}_{suffix}"], errors="coerce")
    for name, src in (("success", "success_rate"), ("failure", "failure_rate")):
        out[f"{name}_base"] = pd.to_numeric(both[f"{src}_base"], errors="coerce") * 100
        out[f"{name}_cand"] = pd.to_numeric(both[f"{src}_cand"], errors="coerce") * 100
        out[f"{name}_pts"] = pd.to_numeric(both[f"{src}_abs"], errors="coerce") * 100
    return out[cols].reset_index(drop=True)


def change_counts(change: pd.DataFrame, pct: str) -> dict:
    """Faster / slower / unchanged endpoint counts and median % change for one percentile."""
    d = pd.to_numeric(change.get(f"{pct}_abs", pd.Series(dtype=float)), errors="coerce").dropna()
    p = pd.to_numeric(change.get(f"{pct}_pct", pd.Series(dtype=float)), errors="coerce").dropna()
    return {"faster": int((d < 0).sum()), "slower": int((d > 0).sum()),
            "unchanged": int((d == 0).sum()), "measured": int(len(d)),
            "median_pct": float(p.median()) if len(p) else None}


# ============================================================================ D. endpoint comparison
COMPARE_CHARTS = {"side": "Runs side by side", "panels": "All percentiles, one panel per run"}


def endpoint_labels(metrics: pd.DataFrame) -> dict[str, str]:
    """endpoint_id -> display name, falling back to the id and disambiguating duplicate names."""
    names: dict[str, str] = {}
    for ep_id, name in zip(metrics["endpoint_id"].astype(str), metrics["endpoint_name"]):
        if ep_id not in names or names[ep_id] == ep_id:
            names[ep_id] = ep_id if C._missing(name) or not str(name).strip() else str(name)
    counts = pd.Series(list(names.values()), dtype=object).value_counts()
    return {e: (f"{n} ({e})" if counts.get(n, 0) > 1 else n) for e, n in names.items()}


def _run_time(run: dict) -> pd.Timestamp:
    ts = C.parse_ts(run.get("started_at") or run.get("created_at"))
    return pd.Timestamp(ts) if ts else pd.Timestamp("1970-01-01", tz="UTC")


def _notes(warnings: Any) -> str:
    if isinstance(warnings, str):
        items = [warnings] if warnings else []
    elif isinstance(warnings, (list, tuple)):
        items = [str(w) for w in warnings]
    else:
        items = []
    keep = [w for w in items if "treat as indicative only" not in w]   # covered by Tail reliability
    return "; ".join(keep)


def render_endpoint_comparison(db: Any, metrics: pd.DataFrame, chosen_runs: list[str]) -> None:
    """Section D: P50/P95/P99 per endpoint, separated by run, with a comparability verdict."""
    run_meta = {rid: (db.get_run(rid) or {"run_id": rid}) for rid in chosen_runs}
    in_scope = [r for r in chosen_runs if r in set(metrics["run_id"])]
    in_scope.sort(key=lambda r: _run_time(run_meta[r]))                  # oldest first
    max_runs = T.MAX_CATEGORICAL_SERIES
    if len(in_scope) > max_runs:
        st.info(f"Comparing the {max_runs} newest of the {len(in_scope)} selected runs. Remove "
                "runs in the filter above to choose others.", icon=":material/info:")
        in_scope = in_scope[-max_runs:]
    multi = len(in_scope) > 1
    scoped = metrics[metrics["run_id"].isin(in_scope)].copy()
    scoped["endpoint_id"] = scoped["endpoint_id"].astype(str)
    names = endpoint_labels(scoped)
    scoped["endpoint_name"] = scoped["endpoint_id"].map(names)
    titles = {r: short_run_label(run_meta[r]) for r in in_scope}

    present = scoped.groupby("endpoint_id")["run_id"].nunique()
    options = sorted(present.index, key=lambda e: (-present[e], names[e].lower()))
    default = [e for e in options if present[e] == present.max()]   # measured in the most runs
    min_pick = 1 if multi else 2
    picked = st.multiselect(
        "Endpoints to compare", options, default=default[:8], max_selections=12,
        format_func=lambda e: names[e] + (f"  ·  in {present[e]} of {len(in_scope)} runs"
                                          if multi and present[e] < len(in_scope) else ""),
        key="cmp_endpoints_" + "_".join(in_scope),
        help="Each endpoint is measured per run; samples from different runs are never pooled.")
    if len(picked) < min_pick:
        st.info("Pick two or more endpoints to compare." if not multi
                else "Pick at least one endpoint to compare across the selected runs.",
                icon=":material/compare_arrows:")
        return
    comp = scoped[scoped["endpoint_id"].isin(picked)].copy()

    # ---- comparability verdict
    runs = [run_meta[r] for r in in_scope]
    comparable = runs_comparable(runs)
    if not multi:
        st.success("All endpoints come from one run, so they share the same test conditions and are "
                   "ordered by P50. Each endpoint still does different work on the server; read "
                   "tails only where the sample count supports them.", icon=":material/check_circle:")
    elif comparable:
        st.success(f"These {len(in_scope)} runs share the same recorded test conditions, so their "
                   "bars can be compared directly. Endpoints are ordered by P50 in the newest run.",
                   icon=":material/check_circle:")
    else:
        cond_df, differing = condition_table(runs)
        st.warning("**Not ranked — test conditions differ between these runs** ("
                   + ", ".join(differing) + "). Differences between runs may come from the test "
                   "setup rather than the API (cold connections, for example, include TCP/TLS "
                   "setup in every sample). Endpoints are listed alphabetically.",
                   icon=":material/rule:")
        st.dataframe(cond_df[["Run"] + differing], hide_index=True)

    # ---- order (rank only when conditions allow it)
    newest = in_scope[-1]
    if comparable:
        p50 = (comp[comp["run_id"] == newest].drop_duplicates("endpoint_name")
               .set_index("endpoint_name")["p50_ms"])

        def key(n: str) -> tuple:
            v = p50.get(n)
            return (C._missing(v), 0.0 if C._missing(v) else float(v), n.lower())

        order = sorted(comp["endpoint_name"].unique(), key=key)
    else:
        order = sorted(comp["endpoint_name"].unique(), key=str.lower)

    # ---- chart
    if not multi:
        single = (comp.drop_duplicates("endpoint_name").set_index("endpoint_name")
                  .reindex(order).reset_index())
        C.show_chart(C.comparison_bars(single, label_col="endpoint_name",
                                       title="P50, P95 and P99 by endpoint"), key="compare_bars")
    else:
        k1, k2 = st.columns([2, 1.2])
        mode = k1.segmented_control("Chart", list(COMPARE_CHARTS), format_func=COMPARE_CHARTS.get,
                                    default="side", key="cmp_chart_mode") or "side"
        if mode == "side":
            pct = k2.segmented_control("Percentile", list(T.PERCENTILE_LABELS),
                                       format_func=T.PERCENTILE_LABELS.get, default="p50",
                                       key="cmp_pct") or "p50"
            fig = C.run_bars(comp, f"{pct}_ms", in_scope, titles, order,
                             title=f"{T.PERCENTILE_LABELS[pct]} by endpoint, one bar per run",
                             x_title=f"{T.PERCENTILE_LABELS[pct]} latency (ms)")
        else:
            fig = C.comparison_bars_by_run(comp, in_scope, titles, order,
                                           title="P50, P95 and P99 by endpoint, one panel per run")
        C.show_chart(fig, key="compare_bars")
        have = set(zip(comp["endpoint_id"], comp["run_id"]))
        gaps = {r: [names[e] for e in picked if (e, r) not in have] for r in in_scope}
        gaps = {r: eps for r, eps in gaps.items() if eps}
        if gaps:
            st.caption(":material/info: A missing bar means the run did not measure that endpoint. "
                       + " ".join(f"**{titles[r]}** has no {', '.join(eps)}." for r, eps in gaps.items()))

    # ---- table
    rank = {n: i for i, n in enumerate(order)}
    run_rank = {r: i for i, r in enumerate(in_scope)}
    comp = comp.assign(_o=comp["endpoint_name"].map(rank), _r=comp["run_id"].map(run_rank))
    comp = comp.sort_values(["_o", "_r"])
    warnings = comp["warnings"] if "warnings" in comp.columns else pd.Series([None] * len(comp))
    tbl = pd.DataFrame({
        "Endpoint": comp["endpoint_name"].to_numpy(),
        "Run": comp["run_id"].map(titles).to_numpy(),
        "Samples": pd.to_numeric(comp["samples"], errors="coerce").to_numpy(),
        "P50 (ms)": pd.to_numeric(comp["p50_ms"], errors="coerce").to_numpy(),
        "P95 (ms)": pd.to_numeric(comp["p95_ms"], errors="coerce").to_numpy(),
        "P99 (ms)": pd.to_numeric(comp["p99_ms"], errors="coerce").to_numpy(),
        "Success rate": (pd.to_numeric(comp["success_rate"], errors="coerce") * 100).to_numpy(),
        "Failures": pd.to_numeric(comp["failed"], errors="coerce").to_numpy(),
        "Timeouts": pd.to_numeric(comp["timeouts"], errors="coerce").to_numpy(),
        "Tail reliability": [C.reliability_label(s) for s in comp["samples"]],
        "Notes": [_notes(w) for w in warnings],
    })
    if not multi:
        tbl = tbl.drop(columns=["Run"])
    if not tbl["Notes"].astype(bool).any():
        tbl = tbl.drop(columns=["Notes"])
    num = st.column_config.NumberColumn
    st.dataframe(tbl, hide_index=True, column_config={
        "Samples": num(format="%d", help="Successful, non-warm-up requests used for percentiles"),
        "P50 (ms)": num(format="%.1f"), "P95 (ms)": num(format="%.1f"),
        "P99 (ms)": num(format="%.1f"), "Success rate": num(format="%.1f%%"),
        "Failures": num(format="%d"), "Timeouts": num(format="%d"),
        "Tail reliability": st.column_config.TextColumn(
            help="P95 needs ≥ 20 samples, P99 needs ≥ 100 samples to be meaningful"),
        "Notes": st.column_config.TextColumn(width="large"),
    })
    st.caption("Percentiles use each run's successful, non-warm-up requests only; failures and "
               "timeouts are counted separately. Hover a bar for its sample count.")
