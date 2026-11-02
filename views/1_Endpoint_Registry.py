"""Endpoint registry: browse, inspect, enable for benchmark, import, and manual latency entry."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from src.ui import components as C  # noqa: E402
from src.ui import nav  # noqa: E402
from src.ui.state import (  # noqa: E402
    clear_endpoint_cache, current_identity, get_db, get_endpoints, get_selection, get_settings, set_selection,
)
from src.ui.theme import apply_theme  # noqa: E402

apply_theme("Endpoint registry",
            subtitle="Every documented TradeSmart API V2 endpoint, with its safety class.")

from src.safety import classify, is_auto_benchmarkable  # noqa: E402

SAFETY_LABELS = {"read_only": "Read-only", "state_changing": "State-changing",
                 "special_setup": "Special setup"}
WORK_KEY = "registry_working"
ADMIN_NOTE = "Only admins can change the registry."
can_edit = current_identity().is_admin

# ----------------------------------------------------------------------------- load
try:
    saved = get_endpoints()
except Exception as exc:
    st.error(f"Couldn't load `config/endpoints.json` ({exc}). Fix the file or import a valid "
             "registry below, then reload.", icon=":material/error:")
    saved = []

if WORK_KEY not in st.session_state:
    st.session_state[WORK_KEY] = [ep for ep in saved]
working: list = st.session_state[WORK_KEY]

with st.expander("Safety classes — what runs automatically", expanded=False, icon=":material/shield:"):
    st.markdown(
        f"""
{C.pill("Read-only", "success")} &nbsp; Reads data only. Eligible for controlled, rate-limited
automatic benchmarks.

{C.pill("Special setup", "warning")} &nbsp; Needs a fixture (for example an existing order number) or a
WebSocket session. Runs only after you supply the fixture and confirm.

{C.pill("State-changing", "failure")} &nbsp; Places, modifies, cancels or exits orders, or exchanges a
one-time auth code. **Never executed automatically.** Register it for documentation and record
latency by manual entry.
""",
        unsafe_allow_html=True,
    )

tab_list, tab_detail, tab_import, tab_manual = st.tabs(
    ["Endpoints", "Endpoint details", "Import JSON", "Manual latency entry"])

# ----------------------------------------------------------------------------- list
with tab_list:
    if not working:
        C.empty_state("No endpoints registered",
                      "The registry is empty. Import a registry JSON in the **Import JSON** tab, or "
                      "restore `config/endpoints.json` from the project.")
    else:
        cats = sorted({ep.category for ep in working})
        methods = sorted({ep.method for ep in working})
        f1, f2, f3, f4 = st.columns([2, 1, 1.4, 2])
        f_cat = f1.multiselect("Category", cats, placeholder="All categories")
        f_method = f2.multiselect("Method", methods, placeholder="All")
        f_safety = f3.multiselect("Safety class", list(SAFETY_LABELS),
                                  format_func=SAFETY_LABELS.get, placeholder="All")
        f_text = f4.text_input("Search", placeholder="Name, ID or path")

        def _keep(ep) -> bool:
            if f_cat and ep.category not in f_cat:
                return False
            if f_method and ep.method not in f_method:
                return False
            if f_safety and classify(ep) not in f_safety:
                return False
            q = f_text.strip().lower()
            return not q or q in f"{ep.id} {ep.name} {ep.path}".lower()

        view_eps = [ep for ep in working if _keep(ep)]
        view = pd.DataFrame([{
            "Benchmark": bool(ep.benchmark_enabled and is_auto_benchmarkable(ep)),
            "ID": ep.id, "Name": ep.name, "Category": ep.category, "Method": ep.method,
            "Path": ep.path or f"/{ep.id}",
            "Safety class": SAFETY_LABELS.get(classify(ep), classify(ep)),
            "Auto-benchmarkable": is_auto_benchmarkable(ep),
            "Needs fixture": bool(ep.requires_fixture),
        } for ep in view_eps])

        editor_key = "registry_editor_" + str(abs(hash((tuple(f_cat), tuple(f_method),
                                                        tuple(f_safety), f_text))))
        ids_in_view = [ep.id for ep in view_eps]

        def _apply_edits() -> None:
            state = st.session_state.get(editor_key) or {}
            by_id = {ep.id: ep for ep in st.session_state[WORK_KEY]}
            blocked = []
            for row, change in (state.get("edited_rows") or {}).items():
                if "Benchmark" not in change or int(row) >= len(ids_in_view):
                    continue
                ep = by_id[ids_in_view[int(row)]]
                want = bool(change["Benchmark"])
                if want and not is_auto_benchmarkable(ep):
                    blocked.append(ep.id)
                    ep.benchmark_enabled = False
                else:
                    ep.benchmark_enabled = want
            if blocked:
                st.session_state["_registry_flash"] = (
                    "Not enabled: " + ", ".join(blocked) + ". State-changing endpoints are never "
                    "benchmarked automatically. Use Manual latency entry instead.")

        if not view_eps:
            st.info("No endpoints match these filters. Clear a filter to see more.",
                    icon=":material/filter_alt_off:")
        else:
            st.data_editor(
                view, key=editor_key, hide_index=True, on_change=_apply_edits if can_edit else None,
                disabled=[c for c in view.columns if c != "Benchmark"] if can_edit else True,
                column_config={
                    "Benchmark": st.column_config.CheckboxColumn(
                        help="Include in automated benchmarks by default. Only read-only endpoints "
                             "can be enabled."),
                    "Auto-benchmarkable": st.column_config.CheckboxColumn(disabled=True),
                    "Needs fixture": st.column_config.CheckboxColumn(disabled=True),
                },
            )
        if flash := st.session_state.pop("_registry_flash", None):
            st.warning(flash, icon=":material/block:")

        dirty = {ep.id: ep.benchmark_enabled for ep in working} != \
                {ep.id: ep.benchmark_enabled for ep in saved}
        st.caption(f"{len(view_eps)} of {len(working)} endpoints shown · "
                   f"{sum(1 for ep in working if ep.benchmark_enabled and is_auto_benchmarkable(ep))}"
                   " enabled for benchmark" + (" · **unsaved changes**" if dirty else ""))
        if not can_edit:
            st.caption(ADMIN_NOTE)
        else:
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button("Save registry", type="primary", disabled=not dirty, icon=":material/save:"):
                try:
                    from src.endpoint_registry import save_endpoints

                    save_endpoints(working)
                    get_db().upsert_endpoints(working)
                    clear_endpoint_cache()
                    st.session_state.pop(WORK_KEY, None)
                    st.toast("Registry saved to config/endpoints.json", icon=":material/check:")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Couldn't save the registry: {exc}. Check the file isn't open in another "
                             "program, then try again.", icon=":material/error:")
            if b2.button("Discard changes", disabled=not dirty):
                st.session_state.pop(WORK_KEY, None)
                st.rerun()

# ----------------------------------------------------------------------------- details
with tab_detail:
    if working:
        ids = [ep.id for ep in working]
        _, sel_ep = get_selection()
        idx = ids.index(sel_ep) if sel_ep in ids else 0
        ep_id = st.selectbox("Endpoint", ids, index=idx,
                             format_func=lambda i: f"{next(e.name for e in working if e.id == i)} ({i})")
        ep = next(e for e in working if e.id == ep_id)
        if ep_id != sel_ep:
            set_selection(endpoint_id=ep_id)
        cls = classify(ep)
        kind = {"read_only": "success", "state_changing": "failure"}.get(cls, "warning")
        st.markdown(f"### {ep.name} &nbsp; {C.pill(SAFETY_LABELS.get(cls, cls), kind)}",
                    unsafe_allow_html=True)
        meta = pd.DataFrame([
            ("Method / path", f"{ep.method} {ep.path or '/' + ep.id}"),
            ("Category", ep.category),
            ("Authentication", "Bearer access token" if ep.auth_required else "Not required"),
            ("Auto-benchmarkable", "Yes" if is_auto_benchmarkable(ep) else "No"),
            ("Needs fixture", ("Yes — " + ep.fixture_notes) if ep.requires_fixture else "No"),
            ("Rate limit", " · ".join(x for x in [
                f"{ep.rate_limit_per_sec:g}/s" if ep.rate_limit_per_sec else "",
                f"{ep.rate_limit_per_min:g}/min" if ep.rate_limit_per_min else ""] if x)
             or "Not documented (conservative default applies)"),
            ("Response", ep.response_format or C.DASH),
            ("Doc reference", ep.doc_reference or C.DASH),
        ], columns=["Field", "Value"])
        st.dataframe(meta, hide_index=True)
        if is_auto_benchmarkable(ep):
            if st.button("Check this endpoint", icon=":material/bolt:",
                         help="Send a few requests to this endpoint and see the response."):
                set_selection(endpoint_id=ep.id)
                nav.go_to(nav.PAGE_CHECK)
        d1, d2 = st.columns(2)
        with d1:
            st.markdown("**Required headers**")
            if ep.required_headers:
                st.code("\n".join(ep.required_headers), language="text")
            else:
                st.caption("None documented.")
            st.markdown("**Parameters**")
            if ep.params:
                st.dataframe(pd.DataFrame([{
                    "Name": p.name, "Type": p.type, "Required": p.required,
                    "Description": p.description, "Example": p.example} for p in ep.params]),
                    hide_index=True)
            else:
                st.caption("No parameters.")
        with d2:
            st.markdown("**Request body template** (sent as `jData`)")
            st.json(ep.request_body_template or {}, expanded=True)
            st.caption("`{{Placeholders}}` are filled from the Run benchmark page. "
                       "ClientId always comes from your `.env` and is masked on screen.")
        if ep.notes:
            st.markdown(f"**Notes.** {ep.notes}")
        try:
            from src.endpoint_registry import validate_endpoint

            problems = validate_endpoint(ep)
        except Exception:
            problems = []
        if problems:
            st.warning("Definition problems:\n\n- " + "\n- ".join(problems), icon=":material/rule:")

# ----------------------------------------------------------------------------- import
with tab_import:
    if not can_edit:
        st.info(ADMIN_NOTE, icon=":material/lock:")
    else:
        st.markdown("Import a full registry (`{\"endpoints\": [...]}`) or a bare list of endpoint "
                    "objects. Use the official TradeSmart documentation as the source of truth.")
        upload = st.file_uploader("Registry JSON file", type=["json"])
        pasted = st.text_area("…or paste JSON", height=160, placeholder='{"endpoints": [ … ]}')
        if st.button("Validate import", icon=":material/fact_check:"):
            text = upload.getvalue().decode("utf-8") if upload else pasted
            if not text.strip():
                st.warning("Choose a file or paste JSON first.", icon=":material/upload_file:")
            else:
                try:
                    from src.endpoint_registry import import_endpoints_json, validate_endpoint

                    imported = import_endpoints_json(text)
                    for ep in imported:   # safety: never enable state-changing endpoints on import
                        if not is_auto_benchmarkable(ep):
                            ep.benchmark_enabled = False
                    st.session_state["import_preview"] = imported
                    st.session_state["import_problems"] = {
                        ep.id: validate_endpoint(ep) for ep in imported if validate_endpoint(ep)}
                except Exception as exc:
                    st.session_state.pop("import_preview", None)
                    st.error(f"That JSON couldn't be imported: {exc}. Fix the highlighted part and "
                             "validate again.", icon=":material/error:")
        preview = st.session_state.get("import_preview")
        if preview:
            problems = st.session_state.get("import_problems") or {}
            existing = {ep.id for ep in working}
            new_ids = [ep.id for ep in preview if ep.id not in existing]
            st.success(f"{len(preview)} endpoints parsed · {len(new_ids)} new · "
                       f"{len(preview) - len(new_ids)} update existing IDs.", icon=":material/check:")
            if problems:
                st.warning("Validation problems:\n\n" + "\n".join(
                    f"- **{k}**: {'; '.join(v)}" for k, v in problems.items()), icon=":material/rule:")
            st.dataframe(pd.DataFrame([{"ID": ep.id, "Name": ep.name, "Category": ep.category,
                                        "Safety class": SAFETY_LABELS.get(classify(ep), classify(ep))}
                                       for ep in preview]), hide_index=True)

            def _commit(merged: list) -> None:
                from src.endpoint_registry import save_endpoints

                save_endpoints(merged)
                get_db().upsert_endpoints(merged)
                clear_endpoint_cache()
                for k in (WORK_KEY, "import_preview", "import_problems"):
                    st.session_state.pop(k, None)

            i1, i2, _ = st.columns([1.2, 1.2, 3])
            if i1.button("Merge into registry", type="primary", disabled=bool(problems)):
                by_id = {ep.id: ep for ep in working}
                by_id.update({ep.id: ep for ep in preview})
                _commit(list(by_id.values()))
                st.rerun()
            if i2.button("Replace registry", disabled=bool(problems)):
                _commit(list(preview))
                st.rerun()
            if problems:
                st.caption("Fix the validation problems before saving.")

# ----------------------------------------------------------------------------- manual entry
with tab_manual:
    st.markdown(f"{C.pill('DOCUMENTATION-ONLY / MANUAL ENTRY', 'warning')}", unsafe_allow_html=True)
    st.markdown(
        "Order and other state-changing endpoints are never called by this app. If you measured "
        "one yourself — in a safe sandbox or from your own order logs — record it here. Manual "
        "entries are stored as separate *manual* runs and never mixed into automated metrics.")
    manual_eps = [ep for ep in working if not is_auto_benchmarkable(ep) or ep.requires_fixture]
    if not can_edit:
        st.info("Only admins can enter manual results.", icon=":material/lock:")
    if not manual_eps:
        st.info("No state-changing or special-setup endpoints are registered.",
                icon=":material/info:")
    else:
        settings = get_settings()
        if can_edit:
            with st.form("manual_entry", clear_on_submit=True):
                m_ep = st.selectbox("Endpoint", [ep.id for ep in manual_eps],
                                    format_func=lambda i: f"{next(e.name for e in manual_eps if e.id == i)} ({i})")
                c1, c2, c3 = st.columns(3)
                m_lat = c1.number_input("Observed latency (ms)", min_value=0.0, value=None,
                                        step=1.0, format="%.1f", placeholder="e.g. 182.4")
                m_status = c2.number_input("HTTP status", min_value=100, max_value=599, value=200,
                                           step=1)
                m_ok = c3.radio("Result", ["Success", "Failure"], horizontal=True)
                m_notes = st.text_area("How was this measured?",
                                       placeholder="Sandbox order at 10:42 IST, timed with …")
                submitted = st.form_submit_button("Save manual result", type="primary",
                                                  icon=":material/save:")
            if submitted:
                if m_lat is None:
                    st.error("Enter the observed latency in milliseconds, then save again.",
                             icon=":material/error:")
                else:
                    try:
                        from src.benchmark_engine import record_manual_result

                        ep = next(e for e in manual_eps if e.id == m_ep)
                        rid = record_manual_result(get_db(), ep, float(m_lat), int(m_status),
                                                   m_ok == "Success", settings.environment,
                                                   notes=m_notes)
                        st.success(f"Saved manual result for {ep.name} (run `{rid}`).",
                                   icon=":material/check_circle:")
                    except Exception as exc:
                        st.error(f"Couldn't save the manual result: {exc}. Try again; if it keeps "
                                 "failing, check the database path on the Settings page.",
                                 icon=":material/error:")
        try:
            manual_runs = get_db().list_runs(include_synthetic=False, run_types=["manual"])
        except Exception:
            manual_runs = pd.DataFrame()
        if manual_runs is not None and len(manual_runs):
            st.markdown("**Recent manual entries**")
            rows = get_db().get_results(run_ids=manual_runs["run_id"].head(50).tolist())
            if len(rows):
                show = rows[["started_at", "endpoint_name", "latency_ms", "http_status", "success"]]
                show = show.assign(started_at=[C.fmt_ts(x) for x in show["started_at"]])
                st.dataframe(show.rename(columns={
                    "started_at": "Recorded", "endpoint_name": "Endpoint",
                    "latency_ms": "Latency (ms)", "http_status": "HTTP", "success": "Success"}),
                    hide_index=True)
