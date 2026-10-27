"""Historical performance (E): trends across runs and a baseline-vs-candidate run comparison."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from src.metrics import comparable_conditions, compare_runs  # noqa: E402
from src.ui import analytics as A  # noqa: E402
from src.ui import components as C  # noqa: E402
from src.ui import nav  # noqa: E402
from src.ui import theme as T  # noqa: E402
from src.ui.data import run_endpoint_metrics, runs_frame  # noqa: E402
from src.ui.state import get_db, get_selection, include_synthetic_toggle, load_demo_data  # noqa: E402
from src.ui.theme import apply_theme  # noqa: E402

apply_theme("Historical comparison",
            subtitle="See how latency moves between runs, and what changed.")
include_synth = include_synthetic_toggle()
db = get_db()

runs = runs_frame(db, include_synth, ["http", "demo"])
if len(runs) < 2:
    C.empty_state(
        "Two runs needed" if len(runs) == 1 else "No runs to compare yet",
        "History starts with two benchmark runs. Run the same endpoints again later (ideally with "
        "the same settings), or load synthetic demo data to try this page.",
        icon=":material/history:")
    e1, e2, _ = st.columns([1, 1, 3])
    if e1.button("Load demo data", type="primary", icon=":material/science:"):
        load_demo_data()
        st.rerun()
    with e2:
        nav.nav_link(nav.PAGE_RUN, "Run a benchmark", ":material/play_circle:")
    st.stop()

run_records = runs.to_dict("records")                       # newest first
runs_by_id = {r["run_id"]: r for r in run_records}
run_ids = runs["run_id"].tolist()
short = {rid: A.short_run_label(r) for rid, r in runs_by_id.items()}
kinds = {rid: A.run_kind(r) for rid, r in runs_by_id.items()}
C.data_origin_banner(runs["is_synthetic"])

hist = run_endpoint_metrics(db, run_ids)
if len(hist):
    hist["endpoint_id"] = hist["endpoint_id"].astype(str)

tab_trend, tab_compare = st.tabs(["Trends across runs", "Compare two runs"])

# ============================================================================ trends
with tab_trend:
    if not len(hist):
        C.empty_state("No stored metrics", "These runs have no completed requests yet.")
    else:
        meta = runs[["run_id", "run_time", "is_synthetic", "connection_mode"]]
        trend = hist.drop(columns=[c for c in ("is_synthetic", "connection_mode", "run_time")
                                   if c in hist.columns]).merge(meta, on="run_id", how="left")
        trend["mode"] = trend["connection_mode"].fillna("unknown").astype(str)
        trend["hover"] = trend["run_id"].map(lambda r: f"{short.get(r, r)} · {kinds.get(r, '')}")
        names = A.endpoint_labels(trend)
        counts = trend.groupby("endpoint_id")["run_id"].nunique().sort_values(ascending=False)
        modes = sorted(trend["mode"].unique())
        _, sel_ep = get_selection()
        default = [sel_ep] if sel_ep in counts.index else counts.index[:3].tolist()

        t1, t2, t3 = st.columns([3, 1.3, 1.2])
        chosen = t1.multiselect("Endpoints", counts.index.tolist(), default=default,
                                format_func=lambda e: f"{names.get(e, e)} ({counts[e]} runs)",
                                max_selections=T.MAX_CATEGORICAL_SERIES,
                                help=f"Up to {T.MAX_CATEGORICAL_SERIES} endpoints at once.")
        mode_opts = (["all"] + modes) if len(modes) > 1 else modes
        latest_mode = next((A.run_connection(r) for r in run_records
                            if A.run_connection(r) in modes and not r.get("is_synthetic")), None)
        mode = t2.selectbox(
            "Connection mode", mode_opts,
            index=mode_opts.index(latest_mode) if latest_mode in mode_opts else 0,
            format_func=lambda m: "All modes (separate lines)" if m == "all" else m,
            help="Warm and cold runs measure different things (cold includes TCP/TLS setup in "
                 "every request), so they are never joined into one line.")
        pct = t3.segmented_control("Percentile", list(T.PERCENTILE_LABELS), default="p95",
                                   format_func=T.PERCENTILE_LABELS.get, key="trend_pct",
                                   disabled=len(chosen) == 1) or "p95"
        if not chosen:
            st.info("Pick at least one endpoint to draw its trend.", icon=":material/show_chart:")
        else:
            data = trend[trend["endpoint_id"].isin(chosen)].copy()
            if mode != "all":
                data = data[data["mode"] == mode]
            split = mode == "all" and data["mode"].nunique() > 1
            dashes: dict[str, str] = {}

            def series_name(base: str, m: str) -> str:
                return f"{base} · {m}" if split else base

            if not len(data):
                st.info(f"The selected endpoints have no {mode} runs. Pick another connection mode.",
                        icon=":material/filter_alt_off:")
            else:
                if len(chosen) == 1:
                    long = data.melt(id_vars=["run_id", "run_time", "is_synthetic", "mode", "hover"],
                                     value_vars=["p50_ms", "p95_ms", "p99_ms"], var_name="pct",
                                     value_name="ms")
                    long["key"] = long["pct"].str.replace("_ms", "")
                    long["series"] = [series_name(T.PERCENTILE_LABELS[k], m)
                                      for k, m in zip(long["key"], long["mode"])]
                    colors = {series_name(T.PERCENTILE_LABELS[k], m): T.PERCENTILE_COLORS[k]
                              for k in T.PERCENTILE_LABELS for m in modes}
                    plot, value_col = long, "ms"
                    title = f"{names.get(chosen[0], chosen[0])}: P50, P95, P99 by run"
                else:
                    data["series"] = [series_name(names.get(e, e), m)
                                      for e, m in zip(data["endpoint_id"], data["mode"])]
                    colors = {series_name(names.get(e, e), m): T.series_color(i)
                              for i, e in enumerate(chosen) for m in modes}
                    plot, value_col = data, f"{pct}_ms"
                    title = f"{T.PERCENTILE_LABELS[pct]} by run"
                if split:
                    dashes = {s: "dot" for s in plot["series"].unique() if s.endswith("· cold")}
                    title += " (dotted = cold connections)"
                C.show_chart(C.trend_lines(plot, "series", value_col, title=title, colors=colors,
                                           dashes=dashes), key="trend_chart")
                st.caption("Each point is one run's percentile for that endpoint, computed from that "
                           "run's successful samples only. Hollow markers are synthetic demo runs.")
                small = data[pd.to_numeric(data["samples"], errors="coerce").fillna(0) < 100]
                if len(small):
                    st.caption(f":material/info: {small['run_id'].nunique()} run(s) have fewer than "
                               "100 samples for a selected endpoint, so their P99 is indicative only.")
                tbl = pd.DataFrame({
                    "Run": data["run_id"].map(short), "Kind": data["run_id"].map(kinds),
                    "Endpoint": data["endpoint_id"].map(names), "Connection": data["mode"],
                    "Samples": data["samples"], "P50 (ms)": data["p50_ms"],
                    "P95 (ms)": data["p95_ms"], "P99 (ms)": data["p99_ms"],
                    "Success rate": pd.to_numeric(data["success_rate"], errors="coerce") * 100,
                    "_t": data["run_time"]}).sort_values(["Endpoint", "_t"]).drop(columns="_t")
                num = st.column_config.NumberColumn
                with st.expander("Table view"):
                    st.dataframe(tbl, hide_index=True, column_config={
                        "Samples": num(format="%d"), "P50 (ms)": num(format="%.1f"),
                        "P95 (ms)": num(format="%.1f"), "P99 (ms)": num(format="%.1f"),
                        "Success rate": num(format="%.1f%%")})

# ============================================================================ compare runs
BASE_KEY, CAND_KEY = "hist_baseline", "hist_candidate"


def _swap_runs() -> None:
    st.session_state[BASE_KEY], st.session_state[CAND_KEY] = (st.session_state.get(CAND_KEY),
                                                              st.session_state.get(BASE_KEY))


def _run_option(rid: str) -> str:
    r = runs_by_id[rid]
    n = r.get("endpoint_count")
    eps = "" if C._missing(n) else f" · {int(n)} endpoint{'s' if int(n) != 1 else ''}"
    return f"{short[rid]} · {kinds[rid]}{eps}"


with tab_compare:
    eps_by_run = (hist.groupby("run_id")["endpoint_id"].agg(set).to_dict() if len(hist) else {})
    d_base, d_cand = A.default_run_pair(run_records, eps_by_run)
    if st.session_state.get(CAND_KEY) not in run_ids:
        st.session_state[CAND_KEY] = d_cand or run_ids[0]
    if st.session_state.get(BASE_KEY) not in run_ids:
        st.session_state[BASE_KEY] = d_base or next(r for r in run_ids
                                                    if r != st.session_state[CAND_KEY])

    c1, c_swap, c2 = st.columns([10, 1, 10], vertical_alignment="bottom")
    base_id = c1.selectbox("Baseline run (before)", run_ids, key=BASE_KEY, format_func=_run_option,
                           help="The reference run. Changes are candidate − baseline.")
    c_swap.button("", icon=":material/swap_horiz:", on_click=_swap_runs, help="Swap runs",
                  key="hist_swap")
    cand_id = c2.selectbox("Candidate run (after)", run_ids, key=CAND_KEY, format_func=_run_option,
                           help="The run you are checking against the baseline.")
    if (base_id, cand_id) != (d_base, d_cand) and d_base and d_cand:
        st.caption(f":material/lightbulb: Suggested pair: {short[d_base]} → {short[d_cand]} "
                   "(latest benchmark and the previous run with matching conditions).")
    if base_id == cand_id:
        st.info("Pick two different runs to compare.", icon=":material/compare_arrows:")
        st.stop()

    base_run, cand_run = db.get_run(base_id), db.get_run(cand_id)
    m1, _, m2 = st.columns([10, 1, 10])
    with m1:
        C.run_meta_block(base_run, request_count=runs_by_id[base_id].get("request_count"))
    with m2:
        C.run_meta_block(cand_run, request_count=runs_by_id[cand_id].get("request_count"))
    st.write("")

    # Real vs synthetic is never a performance change: show the numbers but no verdict colours.
    judge = bool(base_run.get("is_synthetic")) == bool(cand_run.get("is_synthetic"))
    diffs = comparable_conditions(base_run, cand_run)
    if kinds[base_id] != kinds[cand_id]:
        diffs = [f"Run kind differs: {kinds[base_id]} vs {kinds[cand_id]}"] + diffs
    if diffs:
        st.warning("**Conditions differ — read changes with care.** Differences may come from the "
                   "test setup rather than the API:\n\n- " + "\n- ".join(diffs),
                   icon=":material/rule:")
    else:
        st.success("Both runs used the same recorded test conditions, so changes below reflect the "
                   "API and network rather than the test setup.", icon=":material/check_circle:")

    base_m = hist[hist["run_id"] == base_id] if len(hist) else pd.DataFrame()
    cand_m = hist[hist["run_id"] == cand_id] if len(hist) else pd.DataFrame()
    cmp = compare_runs(base_m, cand_m)
    if not len(cmp):
        C.empty_state("Nothing to compare", "Neither run has endpoint metrics yet.")
        st.stop()
    names = A.endpoint_labels(pd.concat([base_m, cand_m], ignore_index=True))
    cmp["endpoint_name"] = cmp["endpoint_id"].astype(str).map(names)
    change = A.run_change_frame(cmp, base_m, cand_m)

    if not len(change):
        st.error("**These runs have no endpoints in common**, so there is nothing to compare side "
                 "by side. Pick runs that measured the same endpoints"
                 + (f" — for example {short[d_base]} → {short[d_cand]}." if d_base and d_cand
                    else "."), icon=":material/link_off:")
    else:
        pct = st.segmented_control("Percentile", list(T.PERCENTILE_LABELS), default="p50",
                                   format_func=T.PERCENTILE_LABELS.get, key="hist_cmp_pct") or "p50"
        label = T.PERCENTILE_LABELS[pct]
        stats = A.change_counts(change, pct)
        only_n = int((cmp["status"] != "both").sum())
        med = stats["median_pct"]
        C.card_row([
            dict(label="Endpoints in both runs", value=C.fmt_int(len(change)), tone="accent",
                 sub=f"{only_n} only in one run" if only_n else "Same endpoint set"),
            dict(label=f"Faster at {label}", value=C.fmt_int(stats["faster"]),
                 tone="success" if judge and stats["faster"] else "neutral",
                 sub="▼ lower latency in candidate"),
            dict(label=f"Slower at {label}", value=C.fmt_int(stats["slower"]),
                 tone="failure" if judge and stats["slower"] else "neutral",
                 sub="▲ higher latency in candidate"),
            dict(label=f"Median {label} change", value=C.fmt_signed(med, "%"),
                 sub="across endpoints in both runs",
                 tone="neutral" if not judge or med is None or med == 0
                 else ("success" if med < 0 else "failure")),
        ])
        st.write("")
        change = change.sort_values(f"{pct}_abs", ascending=False, na_position="last")
        C.show_chart(C.change_dumbbell(change, pct, f"Baseline · {short[base_id]}",
                                       f"Candidate · {short[cand_id]}",
                                       title=f"{label} per endpoint: baseline → candidate"),
                     key="cmp_chart")

        st.markdown("#### Change by endpoint")
        view = pd.DataFrame({
            "Endpoint": change["endpoint_name"].to_numpy(),
            "Samples": [f"{C.fmt_int(b)} → {C.fmt_int(c)}"
                        for b, c in zip(change["samples_base"], change["samples_cand"])],
            "P50 Δ ms": change["p50_abs"].to_numpy(), "P50 Δ %": change["p50_pct"].to_numpy(),
            "P95 Δ ms": change["p95_abs"].to_numpy(), "P95 Δ %": change["p95_pct"].to_numpy(),
            "P99 Δ ms": change["p99_abs"].to_numpy(), "P99 Δ %": change["p99_pct"].to_numpy(),
            "Success": [f"{C.fmt_pct(b / 100 if pd.notna(b) else None)} → "
                             f"{C.fmt_pct(c / 100 if pd.notna(c) else None)}"
                             for b, c in zip(change["success_base"], change["success_cand"])],
            "Success Δ": change["success_pts"].to_numpy(),
            "Failure Δ": change["failure_pts"].to_numpy(),
        })
        lat_ms = ["P50 Δ ms", "P95 Δ ms", "P99 Δ ms"]
        lat_pct = ["P50 Δ %", "P95 Δ %", "P99 Δ %"]
        st.dataframe(C.style_change_table(
            view, lower_is_better=(lat_ms + lat_pct + ["Failure Δ"]) if judge else (),
            higher_is_better=["Success Δ"] if judge else (), ms_cols=lat_ms, pct_cols=lat_pct,
            pts_cols=["Success Δ", "Failure Δ"]), hide_index=True)
        colour_note = ("▼ green = improvement (lower latency or failure rate, higher success "
                       "rate); ▲ red = regression." if judge else
                       "No improvement/regression colours: one run is synthetic demo data, so the "
                       "difference is not a TradeSmart performance change.")
        st.caption(f"Δ = candidate − baseline. {colour_note} % change shows n/a when the "
                   "baseline is zero or missing. Rate changes are in percentage points. Sorted by "
                   f"{label} change, largest regression first.")
        small = change[(pd.to_numeric(change["samples_base"], errors="coerce") < 100)
                       | (pd.to_numeric(change["samples_cand"], errors="coerce") < 100)]
        if len(small):
            st.caption(f":material/info: {len(small)} endpoint(s) have fewer than 100 samples in "
                       "at least one run, so P99 changes are indicative only (P95 needs ≥ 20).")

        with st.expander("Baseline and candidate values"):
            values = pd.DataFrame({"Endpoint": change["endpoint_name"].to_numpy()})
            for p in ("p50", "p95", "p99"):
                lab = T.PERCENTILE_LABELS[p]
                values[f"{lab} baseline (ms)"] = change[f"{p}_base"].to_numpy()
                values[f"{lab} candidate (ms)"] = change[f"{p}_cand"].to_numpy()
            values["Success baseline"] = change["success_base"].to_numpy()
            values["Success candidate"] = change["success_cand"].to_numpy()
            values["Failure baseline"] = change["failure_base"].to_numpy()
            values["Failure candidate"] = change["failure_cand"].to_numpy()
            num = st.column_config.NumberColumn
            st.dataframe(values, hide_index=True, column_config={
                c: num(format="%.1f%%" if c.startswith(("Success", "Failure")) else "%.1f")
                for c in values.columns if c != "Endpoint"})

    only = cmp[cmp["status"] != "both"]
    if len(only):
        st.markdown("#### Measured in only one run")
        st.caption("Not compared: these endpoints have no counterpart in the other run.")
        o1, o2 = st.columns(2)
        for col, status, who in ((o1, "baseline_only", "Only in baseline"),
                                 (o2, "candidate_only", "Only in candidate")):
            part = only[only["status"] == status]
            with col:
                st.markdown(f"**{who}** ({len(part)})")
                if not len(part):
                    st.caption("None.")
                    continue
                side = "base" if status == "baseline_only" else "cand"
                st.dataframe(pd.DataFrame({
                    "Endpoint": part["endpoint_name"].to_numpy(),
                    "P50 (ms)": part[f"p50_{side}"].to_numpy(),
                    "P95 (ms)": part[f"p95_{side}"].to_numpy(),
                    "P99 (ms)": part[f"p99_{side}"].to_numpy(),
                    "Success rate": (pd.to_numeric(part[f"success_rate_{side}"], errors="coerce")
                                     * 100).to_numpy()}),
                    hide_index=True, column_config={
                        "P50 (ms)": st.column_config.NumberColumn(format="%.1f"),
                        "P95 (ms)": st.column_config.NumberColumn(format="%.1f"),
                        "P99 (ms)": st.column_config.NumberColumn(format="%.1f"),
                        "Success rate": st.column_config.NumberColumn(format="%.1f%%")})
