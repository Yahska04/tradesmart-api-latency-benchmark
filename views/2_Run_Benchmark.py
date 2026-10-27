"""Configure, preview and run an HTTP latency benchmark against read-only endpoints."""
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
from src.ui.data import NEVER_PROMPT, SECRET_PLACEHOLDERS, discover_placeholders  # noqa: E402
from src.ui.instrument_picker import INSTRUMENT_PLACEHOLDERS, instrument_fields  # noqa: E402
from src.ui.runner import JOB_KEY, LAST_RUN_KEY, active_job, start_job  # noqa: E402
from src.ui.state import (  # noqa: E402
    active_access_token, all_secrets, get_db, get_endpoints, get_logger, get_settings,
    set_selection, token_source,
)
from src.ui.theme import apply_theme  # noqa: E402

apply_theme("Run a benchmark",
            subtitle="Sequential, rate-limited requests to read-only endpoints. Nothing else is sent.")

from src.safety import is_auto_benchmarkable  # noqa: E402

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


# ============================================================================ running job
job = active_job(st.session_state)


@st.fragment(run_every=1.0)
def progress_panel() -> None:
    job = active_job(st.session_state)
    if not job:
        return
    s = job["state"]
    total = max(int(s.get("total") or 0), 1)
    done = int(s.get("completed") or 0)
    st.subheader("Benchmark in progress")
    st.progress(min(done / total, 1.0),
                text=f"{done:,} of {s.get('total') or '…'} requests · "
                     f"{s.get('elapsed_s', 0):.0f}s elapsed · now: {s.get('current') or '—'}")
    m1, m2, m3 = st.columns(3)
    m1.metric("Succeeded", f"{s.get('ok', 0):,}")
    m2.metric("Failed", f"{s.get('failed', 0):,}")
    m3.metric("Run ID", s.get("run_id") or "—")
    if s.get("message"):
        st.caption(s["message"])
    stopping = job["stop"].is_set()
    if st.button("Stopping…" if stopping else "Stop benchmark", type="secondary",
                 icon=":material/stop_circle:", disabled=stopping or s["done"], key="stop_btn"):
        job["stop"].set()
        st.toast("Stopping after the current request…", icon=":material/stop_circle:")
    st.code("\n".join(list(s["log"])[:15]) or "Waiting for the first response…", language="text")
    if s["done"]:
        st.session_state[LAST_RUN_KEY] = {"run_id": s.get("run_id"), "error": s.get("error")}
        st.session_state.pop(JOB_KEY, None)
        st.rerun()


if job:
    progress_panel()
    st.stop()

# ============================================================================ last run summary
last = st.session_state.get(LAST_RUN_KEY)
if last:
    with st.container(border=True):
        if last.get("error"):
            st.error(f"The benchmark stopped with an error: {last['error']}. Results collected "
                     "before the error are saved. Check connectivity and your token, then start "
                     "again.", icon=":material/error:")
        rid = last.get("run_id")
        run = db.get_run(rid) if rid else None
        if run:
            st.subheader("Benchmark finished" if run.get("status") == "completed"
                         else f"Benchmark {run.get('status', 'ended')}")
            results = C.prepare_results(db.get_results(run_ids=[rid]))
            C.run_meta_block(run, request_count=int((~results["is_warmup"]).sum()) if len(results) else 0)
            if len(results):
                from src.metrics import endpoint_metrics, overall_metrics

                om = overall_metrics(results)
                C.card_row([
                    dict(label="Requests", value=C.fmt_int(om.get("total"))),
                    dict(label="Success rate", value=C.fmt_pct(om.get("success_rate")), tone="success"),
                    dict(label="Failures + timeouts", value=C.fmt_int(om.get("failed")),
                         tone="failure" if om.get("failed") else "neutral"),
                    dict(label="Overall P95", value=C.fmt_ms(om.get("p95_ms")), unit="ms",
                         sub=C.sample_size_note(om.get("samples")) and "Indicative only",
                         sub_warn=not om.get("p95_reliable")),
                ])
                em = C.add_categories(endpoint_metrics(results), endpoints)
                C.endpoint_table(em, key="run_summary_table", selectable=False)
            set_selection(run_id=rid)
            a1, a2, _ = st.columns([1.3, 1, 3])
            with a1:
                nav.nav_link(nav.PAGE_ANALYTICS, "Analyse this run", ":material/monitoring:")
            if a2.button("Dismiss summary"):
                st.session_state.pop(LAST_RUN_KEY, None)
                st.rerun()
        elif not last.get("error"):
            st.info("The run ended before any request was stored.", icon=":material/info:")

# ============================================================================ credentials gate
token = active_access_token()
has_token = bool(token)
if not has_token:
    st.error(
        "**No access token yet, so benchmarks can't start.** Get one in either of two ways:\n\n"
        "1. Open **Settings → Get an access token**, sign in through TradeSmart OAuth, and paste "
        "the redirect URL. The token stays in this browser session only.\n"
        "2. Or add `TS_ACCESS_TOKEN=…` and `TS_CLIENT_ID=…` to the `.env` file in the project "
        "folder, then restart the app.\n\nYou can still configure and preview a plan below.",
        icon=":material/key_off:")
    if st.button("Open Settings", icon=":material/settings:"):
        nav.go_to(nav.PAGE_SETTINGS)
else:
    st.caption(f":material/key: Access token loaded from {token_source()} · shown nowhere, "
               "sent only in the Authorization header.")

# ============================================================================ configuration
auto_eps = [ep for ep in endpoints if is_auto_benchmarkable(ep)]
if not auto_eps:
    C.empty_state("No benchmarkable endpoints",
                  "The registry has no read-only endpoints. Review it on the Endpoint registry page.")
    nav.nav_link(nav.PAGE_REGISTRY, "Open endpoint registry", ":material/list_alt:")
    st.stop()

st.subheader("1 · Choose endpoints")
names = {ep.id: ep.name for ep in auto_eps}
def _needs_input(ep) -> bool:
    found = discover_placeholders([ep])
    return any(k not in SECRET_PLACEHOLDERS and k not in NEVER_PROMPT and not v["example"]
               for k, v in found.items())


# Default: enabled endpoints that can run without extra input (more can be added below).
default_ids = [ep.id for ep in auto_eps if ep.benchmark_enabled and not _needs_input(ep)]
selected = st.multiselect(
    "Endpoints", list(names), default=default_ids, format_func=lambda i: f"{names[i]} ({i})",
    help="Only read-only endpoints appear here. Order and other state-changing endpoints are "
         "never executed automatically.")
excluded = len(endpoints) - len(auto_eps)
st.caption(f"{len(auto_eps)} read-only endpoints available · {excluded} state-changing endpoints "
           "hidden (use manual entry on the Endpoint registry page).")

st.subheader("2 · Set test conditions")
c1, c2, c3, c4 = st.columns(4)
iterations = c1.number_input("Iterations per endpoint", 1, 1000,
                             int(_default("default_iterations", 20)), step=10,
                             help="Measured requests per endpoint. P95 needs ≥ 20, P99 needs ≥ 100.")
warmup = c2.number_input("Warm-up requests", 0, 20, int(_default("default_warmup", 2)),
                         help="Sent first, stored, and excluded from metrics.")
timeout_s = c3.number_input("Timeout (s)", 1.0, 120.0, float(_default("default_timeout_s", 10.0)),
                            step=1.0)
delay_ms = c4.number_input("Delay between requests (ms)", 0, 60000,
                           int(_default("default_delay_ms", 500)), step=100)

c5, c6, c7, c8 = st.columns(4)
reuse = c5.toggle("Reuse connections (warm)",
                  value=_default("default_connection_mode", "warm") == "warm",
                  help="On = keep-alive (warm). Off = new connection per request (cold): each "
                       "sample includes DNS, TCP and TLS setup.")
order = c6.radio("Request order", ["round_robin", "grouped"], horizontal=True,
                 format_func={"round_robin": "Interleaved", "grouped": "Grouped"}.get,
                 help="Interleaved spreads time-of-day effects evenly across endpoints.")
max_minutes = c7.number_input("Max duration (min)", 0.0, 240.0, 0.0, step=1.0,
                              help="Hard stop. 0 = no limit.")
safety_factor = c8.slider("Rate-limit safety factor", 0.1, 1.0,
                          float(_default("default_safety_factor", 0.5)), step=0.05,
                          help="Fraction of the documented rate limit to use. 0.5 = half.")

with st.expander("Stop rules and concurrency (advanced)", icon=":material/tune:"):
    s1, s2 = st.columns(2)
    stop_on_rl = s1.checkbox("Stop the whole run on a rate-limit response (429)", value=True)
    max_fail = s2.number_input("Pause an endpoint after N consecutive failures", 1, 50, 5)
    use_conc = st.checkbox("Enable low concurrency", value=False,
                           help="Off by default. Overlapping requests can inflate latency.")
    concurrency = st.slider("Concurrent requests", 2, 4, 2, disabled=not use_conc) if use_conc else 1
    if use_conc:
        st.warning("Concurrent requests can distort latency and approach rate limits. Keep "
                   "sequential mode for comparisons between runs.", icon=":material/warning:")

sel_eps = [ep for ep in auto_eps if ep.id in selected]
placeholders = {k: v for k, v in discover_placeholders(sel_eps).items() if k not in NEVER_PROMPT}
context: dict[str, str] = {}
if placeholders:
    st.subheader("3 · Fill request values")
    st.caption("These fill the `{{placeholders}}` in the selected endpoints' request bodies. "
               "Endpoints with empty required values are blocked in the plan below.")
    instr_names = [n for n in placeholders if n in INSTRUMENT_PLACEHOLDERS]
    if instr_names:
        context.update(instrument_fields(
            "bench_instr", instr_names, endpoint_ids=[ep.id for ep in sel_eps],
            examples={n: placeholders[n].get("example") for n in instr_names}))
    other = [(n, info) for n, info in placeholders.items() if n not in INSTRUMENT_PLACEHOLDERS]
    cols = st.columns(3)
    for i, (name, info) in enumerate(other):
        col = cols[i % 3]
        help_text = (info.get("description") or "") + f" Used by: {', '.join(info['endpoints'])}."
        if name in SECRET_PLACEHOLDERS:
            val = col.text_input(name, value=settings.client_id or "", type="password",
                                 key=f"ctx_{name}", help="Prefilled from TS_CLIENT_ID in .env. "
                                 "Masked on screen and masked in stored run configs. " + help_text)
        else:
            val = col.text_input(name, value=info.get("example") or "", key=f"ctx_{name}",
                                 help=help_text)
        if val.strip():
            context[name] = val.strip()

notes = st.text_input("Run notes (optional)", placeholder="e.g. Office network, after market close")

# ============================================================================ plan preview
from src.benchmark_engine import BenchmarkConfig, build_plan  # noqa: E402

config = BenchmarkConfig(
    endpoint_ids=list(selected), iterations=int(iterations), warmup=int(warmup),
    timeout_s=float(timeout_s), delay_ms=int(delay_ms),
    connection_mode="warm" if reuse else "cold", order=order, concurrency=int(concurrency),
    max_duration_s=float(max_minutes) * 60 if max_minutes else None,
    safety_factor=float(safety_factor), stop_on_rate_limit=bool(stop_on_rl),
    max_consecutive_failures=int(max_fail), payload_context=context,
    environment=settings.environment, notes=notes,
)
problems = config.validate()
plan = build_plan(config, endpoints)

st.subheader("4 · Review the plan")
p1, p2, p3, p4 = st.columns(4)
with p1:
    C.metric_card("Planned requests", C.fmt_int(plan.total_requests),
                  sub=f"{plan.warmup_requests:,} warm-up + {plan.measured_requests:,} measured",
                  tone="accent")
with p2:
    mins, secs = divmod(int(round(plan.estimated_duration_s)), 60)
    C.metric_card("Estimated duration", f"{mins}m {secs:02d}s" if mins else f"{secs}s",
                  sub="Includes delay and rate-limit pacing", tone="accent")
with p3:
    C.metric_card("Endpoints to run", C.fmt_int(len(plan.per_endpoint)),
                  sub=f"{len(plan.blocked)} blocked", tone="failure" if plan.blocked else "neutral")
with p4:
    C.metric_card("Connection mode", config.connection_mode.capitalize(),
                  sub="Sequential" if config.concurrency == 1 else f"Concurrency {config.concurrency}")

if plan.per_endpoint:
    with st.expander("Per-endpoint plan"):
        st.dataframe(pd.DataFrame([{
            "Endpoint": p.get("endpoint_name") or p["endpoint_id"], "Requests": p["requests"],
            "Effective limit (req/s)": p.get("effective_rate_limit"),
            "Documented limit": "Yes" if p.get("rate_limit_documented") else "No (capped)"}
            for p in plan.per_endpoint]), hide_index=True)
if plan.blocked:
    st.error("**Blocked — these won't be sent:**\n\n" + "\n".join(
        f"- **{b['endpoint_id']}**: {b['reason']}" for b in plan.blocked),
        icon=":material/block:")
for w in plan.warnings:
    if settings.environment == "live" and "live" in w.lower():
        continue  # already shown as the banner at the top
    st.warning(w, icon=":material/warning:")
for p in problems:
    st.error(f"Fix this setting: {p}", icon=":material/error:")

# ============================================================================ connectivity + start
st.subheader("5 · Check and start")
cc1, cc2 = st.columns([1, 3])
if cc1.button("Check connectivity", icon=":material/network_check:",
              help="Times DNS, TCP and TLS to the API host. Sends no API request and no credentials."):
    try:
        from src.api_client import TradeSmartClient

        with TradeSmartClient(settings.base_url, timeout_s=float(timeout_s)) as client:
            st.session_state["conn_check"] = client.connectivity_check()
    except Exception as exc:
        st.session_state["conn_check"] = {"ok": False, "error": str(exc)}
cc = st.session_state.get("conn_check")
if cc:
    with cc2:
        if cc.get("ok"):
            st.success(f"Reached {cc.get('host', 'the API host')} · DNS {C.fmt_ms(cc.get('dns_ms'))} ms · "
                       f"TCP {C.fmt_ms(cc.get('connect_ms'))} ms · TLS {C.fmt_ms(cc.get('tls_ms'))} ms",
                       icon=":material/check_circle:")
        else:
            st.error(f"Couldn't reach the API host: {cc.get('error') or 'unknown error'}. Check your "
                     "internet connection, VPN or firewall, then check again.",
                     icon=":material/wifi_off:")

confirm_live = True
if settings.environment == "live":
    confirm_live = st.checkbox("I understand this sends real read-only requests to my live account.")

can_start = has_token and not problems and bool(plan.per_endpoint) and confirm_live
if st.button("Start benchmark", type="primary", icon=":material/play_arrow:", disabled=not can_start):
    from src.api_client import TradeSmartClient

    secrets = all_secrets()
    base_url = settings.base_url

    def client_factory(cfg: BenchmarkConfig) -> TradeSmartClient:
        return TradeSmartClient(base_url, access_token=token, timeout_s=cfg.timeout_s,
                                connection_mode=cfg.connection_mode, secrets=secrets)

    st.session_state.pop(LAST_RUN_KEY, None)
    start_job(st.session_state, db=db, endpoints=endpoints, config=config,
              client_factory=client_factory, base_url=base_url, logger=get_logger())
    st.rerun()
if not can_start:
    reasons = []
    if not has_token:
        reasons.append("add an access token")
    if not plan.per_endpoint:
        reasons.append("select at least one runnable endpoint")
    if problems:
        reasons.append("fix the settings above")
    if not confirm_live:
        reasons.append("confirm the live-account notice")
    if reasons:
        st.caption("To start: " + ", ".join(reasons) + ".")
