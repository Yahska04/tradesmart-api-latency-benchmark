"""TradeSmart API latency benchmark — Streamlit entry point and Overview dashboard (section A).

Run:  streamlit run app.py      (or simply: python app.py — it relaunches itself under Streamlit)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streamlit import runtime  # noqa: E402

if __name__ == "__main__" and not runtime.exists():
    # Started with plain `python app.py` (e.g. VS Code's Run button): hand over to `streamlit run`,
    # otherwise the script executes once in "bare mode" and exits without starting the web server.
    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", str(Path(__file__).resolve()), *sys.argv[1:]]
    sys.exit(stcli.main())

import streamlit as st  # noqa: E402

from src.ui import components as C  # noqa: E402
from src.ui import nav  # noqa: E402
from src.ui.instrument_picker import sidebar_status  # noqa: E402
from src.ui.state import (  # noqa: E402
    HTTP_RUN_TYPES, current_identity, ensure_script_master, get_db, get_endpoints, get_selection,
    include_synthetic_toggle, load_demo_data, set_selection,
)
from src.ui.theme import apply_theme  # noqa: E402

ALL_RUNS = "__all__"


def _load_demo() -> None:
    try:
        n = load_demo_data()
        st.session_state["_flash"] = f"Loaded {n} synthetic demo runs. They are badged SYNTHETIC DEMO."
    except Exception as exc:
        st.session_state["_flash_error"] = (f"Couldn't create demo data ({exc}). Check the database "
                                            "path on the Settings page, then try again.")


def _empty_overview(has_hidden_demo: bool) -> None:
    is_admin = current_identity().is_admin
    if has_hidden_demo:
        C.empty_state(
            "Only demo data so far",
            "No measured runs yet. Demo runs exist but are hidden because *Include synthetic demo "
            "data* is off. Turn it on to explore, or run your first real benchmark.",
            icon=":material/science:",
        )
    else:
        C.empty_state(
            "No benchmark runs yet",
            "Start with demo data to explore the dashboard, or add your TradeSmart credentials "
            "and run a small benchmark against read-only endpoints." if is_admin else
            "Run a small benchmark against read-only endpoints to see results here.",
        )
    c1, c2, c3, _ = st.columns([1, 1, 1, 2])
    if is_admin and not has_hidden_demo:
        c1.button("Load demo data", type="primary", icon=":material/science:", on_click=_load_demo,
                  width="stretch")
    if is_admin and c2.button("Configure credentials", icon=":material/key:", width="stretch"):
        nav.go_to(nav.PAGE_SETTINGS)
    if c3.button("Run a benchmark", icon=":material/play_arrow:", width="stretch"):
        nav.go_to(nav.PAGE_RUN)


def overview() -> None:
    apply_theme("API latency at a glance",
                subtitle="Client-observed P50, P95 and P99 latency for TradeSmart API V2 endpoints.")
    include_synth = include_synthetic_toggle()
    if flash := st.session_state.pop("_flash", None):
        st.success(flash, icon=":material/check_circle:")
    if flash_err := st.session_state.pop("_flash_error", None):
        st.error(flash_err, icon=":material/error:")

    try:
        db = get_db()
        runs = db.list_runs(include_synthetic=include_synth, run_types=HTTP_RUN_TYPES)
    except Exception as exc:  # database unavailable
        st.error(f"Couldn't open the results database ({type(exc).__name__}). Check `TS_DB_PATH` "
                 "in your `.env`, make sure the folder is writable, then reload this page.",
                 icon=":material/database:")
        return

    if runs is None or not len(runs):
        any_runs = db.list_runs(include_synthetic=True, run_types=HTTP_RUN_TYPES)
        _empty_overview(has_hidden_demo=any_runs is not None and len(any_runs) > 0)
        return

    run_ids = runs["run_id"].tolist()
    by_id = {r["run_id"]: r for r in runs.to_dict("records")}
    sel_run, _ = get_selection()
    options = run_ids + ([ALL_RUNS] if len(run_ids) > 1 else [])
    index = options.index(sel_run) if sel_run in options else 0
    choice = st.selectbox(
        "Benchmark run", options, index=index,
        format_func=lambda rid: (f"All {len(run_ids)} listed runs (aggregate)" if rid == ALL_RUNS
                                 else C.run_label(by_id[rid])),
        help="Latest run is selected by default.",
    )
    if choice != ALL_RUNS:
        set_selection(run_id=choice)

    selected_ids = run_ids if choice == ALL_RUNS else [choice]
    results = C.prepare_results(db.get_results(run_ids=selected_ids, include_synthetic=include_synth))
    C.data_origin_banner(results["is_synthetic"] if len(results) else
                         [bool(by_id[r]["is_synthetic"]) for r in selected_ids])

    if choice == ALL_RUNS:
        st.caption(f"Aggregated across {len(selected_ids)} runs. Runs may differ in connection mode, "
                   "configuration and time of day — open Historical comparison for run-by-run detail.")
    else:
        run = db.get_run(choice) or by_id[choice]
        C.run_meta_block(run, request_count=int((~results["is_warmup"]).sum()) if len(results) else 0)

    if not len(results):
        C.empty_state("This run has no results yet",
                      "The run was created but no requests were stored. It may still be running, or "
                      "it was stopped before the first request. Pick another run above or start a "
                      "new benchmark.")
        return

    from src.metrics import overall_metrics

    om = overall_metrics(results)
    st.write("")
    C.card_row([
        dict(label="Endpoints tested", value=C.fmt_int(om.get("endpoints")), tone="accent"),
        dict(label="Requests executed", value=C.fmt_int(om.get("total")),
             sub="Measured, excluding warm-up", tone="accent"),
        dict(label="Success rate", value=C.fmt_pct(om.get("success_rate")),
             sub=f"{C.fmt_int(om.get('successful'))} successful", tone="success"),
        dict(label="Failures + timeouts", value=C.fmt_int(om.get("failed")),
             sub=f"incl. {C.fmt_int(om.get('timeouts'))} timeouts · "
                 f"{C.fmt_int(om.get('rate_limited'))} rate-limited",
             tone="failure" if (om.get("failed") or 0) else "neutral"),
    ], per_row=4)
    st.write("")
    samples = om.get("samples") or 0
    C.card_row([
        dict(label="Overall P50", value=C.fmt_ms(om.get("p50_ms")), unit="ms",
             sub=f"{C.fmt_int(samples)} successful samples"),
        dict(label="Overall P95", value=C.fmt_ms(om.get("p95_ms")), unit="ms",
             sub=None if om.get("p95_reliable") else "Indicative: fewer than 20 samples",
             sub_warn=not om.get("p95_reliable")),
        dict(label="Overall P99", value=C.fmt_ms(om.get("p99_ms")), unit="ms",
             sub=None if om.get("p99_reliable") else "Indicative: fewer than 100 samples",
             sub_warn=not om.get("p99_reliable")),
    ], per_row=3)
    st.write("")
    st.info(
        "**How to read overall percentiles.** They are calculated from every successful, non-warm-up "
        "request sample in the selection, pooled across endpoints. A different mix of endpoints "
        "(or more samples from one endpoint) shifts these numbers. Use the per-endpoint table for "
        "like-for-like figures. Failures and timeouts are excluded from percentiles and counted "
        "separately above.",
        icon=":material/info:",
    )

    if choice != ALL_RUNS:
        from src.metrics import endpoint_metrics

        st.subheader("Endpoints in this run")
        em = C.add_categories(endpoint_metrics(results), get_endpoints())
        em = em.sort_values("endpoint_name", key=lambda s: s.astype(str).str.lower())
        picked = C.endpoint_table(em, key="overview_endpoints")
        st.caption("Select a row to open its latency detail. All endpoints in one run share the same "
                   "test conditions, but each endpoint does different work on the server.")
        if picked:
            set_selection(run_id=choice, endpoint_id=str(picked["endpoint_id"]))
            nav.go_to(nav.PAGE_ANALYTICS)

    st.subheader("Next steps")
    l1, l2, l3, l4 = st.columns(4)
    with l1:
        nav.nav_link(nav.PAGE_ANALYTICS, "Explore endpoint performance", ":material/monitoring:")
    with l2:
        nav.nav_link(nav.PAGE_HISTORY, "Compare runs over time", ":material/history:")
    with l3:
        nav.nav_link(nav.PAGE_RUN, "Run a new benchmark", ":material/play_circle:")
    with l4:
        nav.nav_link(nav.PAGE_METHODOLOGY, "Read the methodology", ":material/menu_book:")


def main() -> None:
    if not current_identity().authenticated:
        # Nothing runs for a denied request: no navigation, status lines or downloads.
        apply_theme("Access denied", show_title=False)
        return
    pages = {
        "Dashboards": [
            st.Page(overview, title="Overview", icon=":material/dashboard:", default=True),
            st.Page(nav.PAGE_ANALYTICS, title="Performance analytics", icon=":material/monitoring:"),
            st.Page(nav.PAGE_HISTORY, title="Historical comparison", icon=":material/history:"),
        ],
        "Benchmark": [
            st.Page(nav.PAGE_CHECK, title="Check an endpoint", icon=":material/bolt:"),
            st.Page(nav.PAGE_ORDER_TEST, title="Order latency test", icon=":material/shopping_cart:"),
            st.Page(nav.PAGE_RUN, title="Run benchmark", icon=":material/play_circle:"),
            st.Page(nav.PAGE_WEBSOCKET, title="WebSocket benchmark", icon=":material/cell_tower:"),
            st.Page(nav.PAGE_REGISTRY, title="Endpoint registry", icon=":material/list_alt:"),
        ],
        "Reference": [
            st.Page(nav.PAGE_SETTINGS, title="Settings", icon=":material/settings:"),
            st.Page(nav.PAGE_METHODOLOGY, title="Methodology", icon=":material/menu_book:"),
        ],
    }
    ensure_script_master()  # background download of the instrument lists, once per server process
    sidebar_status()
    st.navigation(pages).run()


main()
