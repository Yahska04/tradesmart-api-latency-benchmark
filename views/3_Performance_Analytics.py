"""Performance analytics: endpoint table (B), distribution (C), comparison (D), request log (F)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from src.ui import analytics as A  # noqa: E402
from src.ui import components as C  # noqa: E402
from src.ui import nav  # noqa: E402
from src.ui.data import runs_frame  # noqa: E402
from src.ui.state import (  # noqa: E402
    HTTP_RUN_TYPES, all_secrets, current_identity, get_db, get_endpoints, get_selection,
    include_synthetic_toggle, load_demo_data, set_selection,
)
from src.ui.theme import apply_theme  # noqa: E402

apply_theme("Performance analytics",
            subtitle="Per-endpoint percentiles, distributions and every request behind them.")
include_synth = include_synthetic_toggle()
db = get_db()
endpoints = get_endpoints()

VIEWS = ["Endpoint table", "Latency distribution", "Compare endpoints", "Request log"]
VIEW_KEY = "analytics_view"
TABLE_KEY = "analytics_table"

runs = runs_frame(db, include_synth, HTTP_RUN_TYPES)
if not len(runs):
    C.empty_state("Nothing to analyse yet",
                  "Run a benchmark, or load synthetic demo data from the Overview page to explore. "
                  "If you have demo runs, turn on *Include synthetic demo data* in the sidebar.")
    e1, e2, _ = st.columns([1, 1, 3])
    if current_identity().is_admin and e1.button("Load demo data", type="primary", icon=":material/science:"):
        load_demo_data()
        st.rerun()
    with e2:
        nav.nav_link(nav.PAGE_RUN, "Run a benchmark", ":material/play_circle:")
    st.stop()

runs_by_id = {r["run_id"]: r for r in runs.to_dict("records")}
run_ids_all = runs["run_id"].tolist()
sel_run, sel_ep = get_selection()

# ---------------------------------------------------------------- filter row (scopes every view)
f1, f2, f3, f4 = st.columns([3, 1.6, 1, 1.4])
default_runs = [sel_run] if sel_run in runs_by_id else run_ids_all[:1]
chosen_runs = f1.multiselect("Benchmark runs", run_ids_all, default=default_runs,
                             format_func=lambda r: C.run_label(runs_by_id[r]),
                             help="Metrics are always computed per run, never pooled across runs.")
categories = sorted({ep.category for ep in endpoints})
f_cat = f2.multiselect("Category", categories, placeholder="All categories")
f_method = f3.multiselect("Method", ["POST", "GET"], placeholder="All")
f_health = f4.selectbox("Status", list(A.HEALTH_FILTERS), format_func=A.HEALTH_FILTERS.get)

if not chosen_runs:
    st.info("Pick at least one benchmark run above to see results.", icon=":material/filter_alt:")
    st.stop()

results = C.prepare_results(db.get_results(run_ids=chosen_runs, include_synthetic=include_synth))
C.data_origin_banner([bool(runs_by_id[r].get("is_synthetic")) for r in chosen_runs])
if len(chosen_runs) == 1:
    set_selection(run_id=chosen_runs[0])

cat_by_id = {ep.id: ep.category for ep in endpoints}
if len(results):
    results["category"] = results["endpoint_id"].map(cat_by_id).fillna("Unregistered")
    if f_cat:
        results = results[results["category"].isin(f_cat)]
    if f_method:
        results = results[results["method"].isin(f_method)]

metrics = A.per_run_endpoint_metrics(results, runs_by_id, endpoints)
metrics = A.apply_health_filter(metrics, f_health)

if not len(metrics):
    C.empty_state("No endpoints match these filters",
                  "Clear the category, method or status filter, or pick a different run.",
                  icon=":material/filter_alt_off:")
    st.stop()


# ---------------------------------------------------------------- view switcher
def _on_row_select() -> None:
    state = st.session_state.get(TABLE_KEY)
    rows = list((state or {}).get("selection", {}).get("rows", []))
    table = st.session_state.get("_analytics_rows")
    if rows and table is not None and rows[0] < len(table):
        row = table.iloc[rows[0]]
        set_selection(run_id=str(row["run_id"]), endpoint_id=str(row["endpoint_id"]))
        st.session_state[VIEW_KEY] = VIEWS[1]


if st.session_state.get(VIEW_KEY) not in VIEWS:
    st.session_state[VIEW_KEY] = VIEWS[1] if st.query_params.get("endpoint") else VIEWS[0]
view = st.segmented_control("View", VIEWS, key=VIEW_KEY, label_visibility="collapsed")
view = view or VIEWS[0]
st.write("")

# ================================================================ B. endpoint table
if view == VIEWS[0]:
    q = st.text_input("Search endpoints", placeholder="Name or ID", key="analytics_search")
    table = metrics
    if q.strip():
        needle = q.strip().lower()
        table = table[table["endpoint_name"].astype(str).str.lower().str.contains(needle, regex=False)
                      | table["endpoint_id"].astype(str).str.lower().str.contains(needle, regex=False)]
    table = table.reset_index(drop=True)
    st.session_state["_analytics_rows"] = table
    if not len(table):
        st.info(f"No endpoint matches “{q}”. Clear the search to see all endpoints.",
                icon=":material/search_off:")
    else:
        C.endpoint_table(table, key=TABLE_KEY, show_run=len(chosen_runs) > 1,
                         on_select=_on_row_select)
        st.caption("Click a column header to sort · select a row to open its latency detail. "
                   "Percentiles use successful requests only; failures and timeouts are counted "
                   "separately. P95 needs ≥ 20 samples and P99 ≥ 100 to be meaningful.")

# ================================================================ C. latency distribution
elif view == VIEWS[1]:
    run_opts = [r for r in chosen_runs if r in set(metrics["run_id"])]
    d1, d2 = st.columns([2, 2])
    r_index = run_opts.index(sel_run) if sel_run in run_opts else 0
    run_id = d1.selectbox("Run", run_opts, index=r_index,
                          format_func=lambda r: C.run_label(runs_by_id[r]))
    ep_rows = metrics[metrics["run_id"] == run_id]
    ep_ids = ep_rows["endpoint_id"].astype(str).tolist()
    ep_names = dict(zip(ep_ids, ep_rows["endpoint_name"].astype(str)))
    e_index = ep_ids.index(sel_ep) if sel_ep in ep_ids else 0
    endpoint_id = d2.selectbox("Endpoint", ep_ids, index=e_index,
                               format_func=lambda e: f"{ep_names.get(e, e)} ({e})")
    if endpoint_id:
        set_selection(run_id=run_id, endpoint_id=endpoint_id)
        A.render_endpoint_detail(db, results, run_id, endpoint_id, key="detail")

# ================================================================ D. endpoint comparison
elif view == VIEWS[2]:
    if len(chosen_runs) == 1:
        st.caption(":material/info: Add more runs in *Benchmark runs* above to compare the same "
                   "endpoints across runs (for example warm vs cold).")
    A.render_endpoint_comparison(db, metrics, chosen_runs)

# ================================================================ F. request-level results
else:
    from src.exporters import export_filename, metrics_to_csv, results_to_csv

    g1, g2, g3 = st.columns([2, 2, 1])
    ep_choices = sorted(results["endpoint_id"].dropna().astype(str).unique()) if len(results) else []
    r_eps = g1.multiselect("Endpoint", ep_choices, placeholder="All endpoints")
    outcomes = sorted(results["outcome"].dropna().astype(str).unique()) if len(results) else []
    r_out = g2.multiselect("Outcome", outcomes, placeholder="All outcomes")
    with_warmup = g3.checkbox("Include warm-up", value=False)

    rows = results
    if with_warmup:
        rows = C.prepare_results(db.get_results(run_ids=chosen_runs, include_warmup=True,
                                                include_synthetic=include_synth))
        rows["category"] = rows["endpoint_id"].map(cat_by_id).fillna("Unregistered")
        if f_cat:
            rows = rows[rows["category"].isin(f_cat)]
        if f_method:
            rows = rows[rows["method"].isin(f_method)]
    if r_eps:
        rows = rows[rows["endpoint_id"].isin(r_eps)]
    if r_out:
        rows = rows[rows["outcome"].isin(r_out)]

    if not len(rows):
        st.info("No requests match these filters. Clear a filter to see more.",
                icon=":material/filter_alt_off:")
    else:
        show_cols = ["started_at", "endpoint_name", "method", "latency_ms", "http_status", "outcome",
                     "is_timeout", "success", "error_message", "response_bytes", "api_stat",
                     "connection_mode", "is_warmup", "run_id", "source", "is_synthetic"]
        view_df = rows[[c for c in show_cols if c in rows.columns]].copy()
        view_df["started_at"] = [C.fmt_ts(x) for x in view_df["started_at"]]
        limit = 5000
        st.caption(f"{len(view_df):,} requests" + (f" · showing the latest {limit:,}"
                                                   if len(view_df) > limit else ""))
        st.dataframe(view_df.tail(limit).iloc[::-1], hide_index=True, column_config={
            "started_at": "Started (IST)", "endpoint_name": "Endpoint",
            "latency_ms": st.column_config.NumberColumn("Latency (ms)", format="%.2f"),
            "http_status": st.column_config.NumberColumn("HTTP", format="%d"),
            "error_message": "Error (redacted)", "response_bytes": "Bytes"})

    st.markdown("#### Export")
    secrets = all_secrets()
    x1, x2, _ = st.columns([1.2, 1.2, 3])
    with x1:
        C.download_csv_button("Download requests CSV",
                              results_to_csv(rows, secrets) if len(rows) else b"",
                              export_filename("results", chosen_runs), key="dl_results",
                              disabled=not len(rows),
                              help="Filtered request rows. Whitelisted columns, secrets redacted.")
    with x2:
        export_metrics = metrics.drop(columns=["run_label", "item"], errors="ignore")
        C.download_csv_button("Download metrics CSV", metrics_to_csv(export_metrics, secrets),
                              export_filename("metrics", chosen_runs), key="dl_metrics",
                              help="Per-run endpoint metrics for the current filters.")
    st.caption("Exports never include access tokens, keys, client IDs or response bodies.")
