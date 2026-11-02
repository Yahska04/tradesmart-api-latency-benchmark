"""Check one endpoint on demand: send a few timed requests and see exactly what comes back."""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from src.ui import components as C  # noqa: E402
from src.ui import nav  # noqa: E402
from src.ui.data import NEVER_PROMPT, SECRET_PLACEHOLDERS, discover_placeholders  # noqa: E402
from src.ui.instrument_picker import INSTRUMENT_PLACEHOLDERS, instrument_fields  # noqa: E402
from src.ui.state import (  # noqa: E402
    active_access_token, all_secrets, get_db, get_endpoints, get_logger, get_selection, get_settings,
    set_selection, token_source,
)
from src.ui.theme import apply_theme  # noqa: E402

apply_theme("Check an endpoint",
            subtitle="Send a few requests to one endpoint and see exactly what comes back.")

from src.endpoint_check import MAX_CHECK_REQUESTS, PREVIEW_KEEP_KEYS, run_check, save_check  # noqa: E402
from src.endpoint_registry import RegistryError, missing_placeholders, render_payload  # noqa: E402
from src.redaction import redact_mapping  # noqa: E402
from src.safety import UnsafeEndpointError, is_auto_benchmarkable  # noqa: E402

OUTCOME_KEY = "endpoint_check_outcome"

settings = get_settings()
db = get_db()
endpoints = get_endpoints()
C.live_warning_banner(settings.environment)


def _default(key: str, fallback):
    try:
        value = db.get_setting(key, fallback)
        return fallback if value is None else value
    except Exception:
        return fallback


token = active_access_token()
if token:
    st.caption(f":material/key: Access token loaded from {token_source()} · never shown or stored.")
else:
    how = ("An admin can get one on **Settings → Get an access token**." if settings.is_server() else
           "Get one on **Settings → Get an access token**, or add `TS_ACCESS_TOKEN` to `.env` and restart the app.")
    st.error(f"**No access token yet, so requests can't be sent.** {how} You can still pick an endpoint and "
             "preview its request below.", icon=":material/key_off:")
    nav.nav_link(nav.PAGE_SETTINGS, "Open Settings", ":material/settings:")

checkable = sorted((ep for ep in endpoints if is_auto_benchmarkable(ep)), key=lambda e: (e.category, e.name))
if not checkable:
    C.empty_state("No endpoints to check",
                  "The registry has no read-only endpoints. Review it on the Endpoint registry page.")
    nav.nav_link(nav.PAGE_REGISTRY, "Open endpoint registry", ":material/list_alt:")
    st.stop()

# ============================================================================ 1 · pick
st.subheader("1 · Pick an endpoint")
by_id = {ep.id: ep for ep in checkable}
ids = list(by_id)
wanted = get_selection()[1]
start = ids.index(wanted) if wanted in by_id else 0
ep_id = st.selectbox("Endpoint", ids, index=start,
                     format_func=lambda i: f"{by_id[i].category} · {by_id[i].name} ({i})",
                     help="Read-only endpoints only. Order and other state-changing endpoints are never "
                          "sent from this app.")
if ep_id != wanted:
    set_selection(endpoint_id=ep_id)
ep = by_id[ep_id]
hidden = len(endpoints) - len(checkable)
st.caption(f"{len(checkable)} read-only endpoints available · {hidden} state-changing or special-setup "
           "endpoints are not listed and can't be sent.")
nav.nav_link(nav.PAGE_ORDER_TEST, "Measure PlaceOrder / CancelOrder latency (guarded live test)",
             ":material/shopping_cart:")

limit = ("not documented (capped at 5 req/s)" if ep.rate_limit_per_sec is None and ep.rate_limit_per_min is None
         else " · ".join(x for x in (f"{ep.rate_limit_per_sec:g}/s" if ep.rate_limit_per_sec else "",
                                     f"{ep.rate_limit_per_min:g}/min" if ep.rate_limit_per_min else "") if x))
st.markdown(f"{C.pill(ep.method, 'info')} `{ep.path}` {C.pill(ep.category, 'neutral')} "
            f"{C.pill('read-only', 'success')} &nbsp; Rate limit: {C.esc(limit)}", unsafe_allow_html=True)
if ep.requires_fixture or ep.notes or ep.response_format:
    with st.expander("About this endpoint", icon=":material/info:"):
        if ep.requires_fixture:
            st.warning(ep.fixture_notes or "Needs a specific value that only exists in your account.",
                       icon=":material/warning:")
        if ep.response_format:
            st.markdown(f"**Response:** {ep.response_format}")
        if ep.notes:
            st.caption(ep.notes)

# ============================================================================ 2 · request values
placeholders = {k: v for k, v in discover_placeholders([ep]).items() if k not in NEVER_PROMPT}
context: dict[str, str] = {}
st.subheader("2 · Request values")
if placeholders:
    instr_names = [n for n in placeholders if n in INSTRUMENT_PLACEHOLDERS]
    if instr_names:
        context.update(instrument_fields(
            "chk_instr", instr_names, endpoint_ids=[ep.id],
            examples={n: placeholders[n].get("example") for n in instr_names}))
    other = [(n, info) for n, info in placeholders.items() if n not in INSTRUMENT_PLACEHOLDERS]
    cols = st.columns(3)
    for i, (name, info) in enumerate(other):
        col = cols[i % 3]
        if name in SECRET_PLACEHOLDERS:
            val = col.text_input(name, value=settings.client_id or "", type="password", key=f"chk_{name}",
                                 help="Prefilled from TS_CLIENT_ID in .env. Masked on screen.")
        else:
            val = col.text_input(name, value=info.get("example") or "", key=f"chk_{ep.id}_{name}",
                                 help=info.get("description") or None)
        if val.strip():
            context[name] = val.strip()
else:
    st.caption("This endpoint needs no extra values.")

custom_template = None
body_error = None
with st.expander("Edit the request body (JSON)", icon=":material/data_object:"):
    st.caption("Optional. Keep `{{ClientId}}` and other `{{placeholders}}` — they are filled from the "
               "values above, so your client ID is never typed here.")
    edited = st.text_area("Request body template", json.dumps(ep.request_body_template, indent=2),
                          height=180, key=f"chk_body_{ep.id}")
    try:
        parsed = json.loads(edited or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("the body must be a JSON object, e.g. {\"uid\": \"{{ClientId}}\"}")
        if parsed != ep.request_body_template:
            custom_template = parsed
    except ValueError as exc:
        body_error = f"The request body isn't valid JSON ({exc}). Fix it, or clear the box to reset."

run_ep = dataclasses.replace(ep, request_body_template=custom_template) if custom_template else ep
missing = missing_placeholders(run_ep, context)
payload: dict | None = None
if body_error:
    st.error(body_error, icon=":material/error:")
elif missing:
    st.warning("Fill in: " + ", ".join(f"`{m}`" for m in missing), icon=":material/edit:")
else:
    try:
        payload = render_payload(run_ep, context)
    except RegistryError as exc:
        st.error(f"Couldn't build the request: {exc}", icon=":material/error:")
if payload is not None:
    st.caption("Request that will be sent (account identifiers masked here):")
    st.code("jData=" + json.dumps(redact_mapping(payload, all_secrets(), keep_keys=PREVIEW_KEEP_KEYS), indent=2),
            language="json")

# ============================================================================ 3 · send
st.subheader("3 · Send")
s1, s2, s3, s4 = st.columns(4)
n_requests = s1.number_input("Requests", 1, MAX_CHECK_REQUESTS, 3,
                             help=f"1–{MAX_CHECK_REQUESTS}. A quick check, not a benchmark: use Run "
                                  "benchmark for reliable P95/P99.")
delay_ms = s2.number_input("Delay between requests (ms)", 0, 10000, int(_default("default_delay_ms", 500)),
                           step=100)
timeout_s = s3.number_input("Timeout (s)", 1.0, 60.0, float(_default("default_timeout_s", 10.0)), step=1.0)
reuse = s4.toggle("Reuse connections (warm)", value=_default("default_connection_mode", "warm") == "warm",
                  help="Off = new connection per request (cold): each sample includes DNS, TCP and TLS.")
save = st.checkbox("Save these samples to history",
                   help="Stores them as a normal run so they appear in Performance analytics and "
                        "Historical comparison. Off = shown here only.")
confirm_live = True
if settings.environment == "live":
    confirm_live = st.checkbox("I understand this sends real read-only requests to my live account.",
                               key="chk_confirm_live")

can_send = bool(token) and payload is not None and confirm_live
if st.button("Send requests", type="primary", icon=":material/send:", disabled=not can_send):
    from src.api_client import TradeSmartClient
    from src.database import new_run_id

    get_logger()
    secrets = all_secrets()
    mode = "warm" if reuse else "cold"
    bar = st.progress(0.0, text="Sending…")
    done = {"n": 0}

    def _progress(_result) -> None:
        done["n"] += 1
        bar.progress(done["n"] / int(n_requests), text=f"Sent {done['n']} of {int(n_requests)}")

    try:
        with TradeSmartClient(settings.base_url, access_token=token, timeout_s=float(timeout_s),
                              connection_mode=mode, secrets=secrets) as client:
            outcome = run_check(run_ep, payload, client, run_id=new_run_id("check"),
                                requests=int(n_requests), delay_ms=int(delay_ms),
                                environment=settings.environment, secrets=secrets, on_result=_progress)
        saved_run = None
        if save and outcome.results:
            saved_run = save_check(db, run_ep, outcome, base_url=settings.base_url, connection_mode=mode,
                                   timeout_s=float(timeout_s), delay_ms=int(delay_ms),
                                   environment=settings.environment)
        st.session_state[OUTCOME_KEY] = {"endpoint_id": ep.id, "outcome": outcome, "mode": mode,
                                         "saved_run": saved_run}
    except UnsafeEndpointError as exc:
        st.error(f"Not sent: {exc}", icon=":material/block:")
    except Exception as exc:
        st.error(f"The check couldn't run: {exc}. Check your connection and settings, then try again.",
                 icon=":material/error:")
    finally:
        bar.empty()
elif not can_send:
    reasons = []
    if not token:
        reasons.append("add an access token")
    if payload is None:
        reasons.append("complete the request values")
    if not confirm_live:
        reasons.append("confirm the live-account notice")
    st.caption("To send: " + ", ".join(reasons) + ".")

# ============================================================================ results
state = st.session_state.get(OUTCOME_KEY)
if not state or state["endpoint_id"] != ep.id:
    st.stop()

outcome = state["outcome"]
df = outcome.frame()
st.subheader("Results")
if outcome.stop_reason:
    st.warning(outcome.stop_reason, icon=":material/pause_circle:")

ok = df[df["success"].astype(bool)]
lat_ok = ok["latency_ms"].dropna()
last = outcome.results[-1]
c1, c2, c3, c4 = st.columns(4)
with c1:
    C.metric_card("Successful", f"{len(ok)} / {len(df)}",
                  sub="HTTP 2xx and stat Ok", tone="success" if len(ok) == len(df) else "failure")
with c2:
    C.metric_card("Median latency", C.fmt_ms(lat_ok.median() if len(lat_ok) else None), "ms",
                  sub=f"{len(lat_ok)} successful samples · {state['mode']}", tone="accent")
with c3:
    C.metric_card("Fastest / slowest",
                  f"{C.fmt_ms(lat_ok.min())} / {C.fmt_ms(lat_ok.max())}" if len(lat_ok) else C.fmt_ms(None),
                  "ms", sub="Successful requests only")
with c4:
    C.metric_card("Last response", f"HTTP {last.http_status}" if last.http_status else last.outcome,
                  sub=(f"stat {last.api_stat}" if last.api_stat else last.outcome),
                  tone="success" if last.success else "failure")
st.caption("Client-observed latency: network, TLS (cold mode) and server time together. A handful of "
           "requests is a spot check — run a benchmark for percentiles you can rely on.")

table = pd.DataFrame({
    "#": df["seq"],
    "Started": [C.fmt_ts(v) for v in df["started_at"]],
    "Latency (ms)": df["latency_ms"].round(1),
    "HTTP": df["http_status"],
    "Outcome": df["outcome"],
    "stat": df["api_stat"],
    "Message": df["error_message"].fillna(""),
    "Bytes": df["response_bytes"],
    "Server timing": df["server_timing"].fillna(""),
})
st.dataframe(table, hide_index=True, width="stretch")
if len(df) > 1:
    C.show_chart(C.latency_over_time(C.prepare_results(df), title="Latency per request"), key="chk_over_time")

with st.expander("Last response body (redacted)", icon=":material/description:",
                 expanded=not last.success):
    if outcome.preview:
        st.caption(outcome.preview_note + " Shown here only — never saved, logged or exported.")
        st.code(outcome.preview, language="json")
    else:
        st.caption(outcome.preview_note or "No response body was received (network error or timeout).")

if state.get("saved_run"):
    st.success(f"Saved to history as run `{state['saved_run']}`.", icon=":material/check_circle:")
    if st.button("Open in Performance analytics", icon=":material/monitoring:"):
        set_selection(run_id=state["saved_run"], endpoint_id=ep.id)
        nav.go_to(nav.PAGE_ANALYTICS)
