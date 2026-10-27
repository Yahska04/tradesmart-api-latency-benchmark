"""WebSocket market-data benchmark — kept separate from HTTP endpoint latency."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from src.ui import components as C  # noqa: E402
from src.ui import nav  # noqa: E402
from src.ui.state import active_access_token, get_db, get_settings  # noqa: E402
from src.ui.theme import apply_theme  # noqa: E402

apply_theme("WebSocket benchmark",
            subtitle="Connection, acknowledgement and message-arrival timing for the market feed.")
settings = get_settings()
db = get_db()
C.live_warning_banner(settings.environment)

st.info(
    "**Arrival intervals are not exchange latency.** The gap between two feed messages mostly shows "
    "how often TradeSmart publishes updates for an instrument — a quiet stock ticks rarely. Without "
    "trustworthy exchange timestamps we can't measure one-way network or exchange-to-client latency, "
    "so this page never claims to. These metrics are kept apart from HTTP endpoint latency.",
    icon=":material/info:")


def _stat(values: list[float]) -> dict:
    from src.metrics import percentile

    return {"p50_ms": percentile(values, 50), "p95_ms": percentile(values, 95),
            "p99_ms": percentile(values, 99)}


def render_ws_result(summary: dict, intervals: list[float], key: str) -> None:
    C.card_row([
        dict(label="Connect", value=C.fmt_ms(summary.get("connect_ms")), unit="ms",
             sub="TCP + TLS + WebSocket upgrade", tone="accent"),
        dict(label="Auth acknowledged", value=C.fmt_ms(summary.get("auth_ack_ms")), unit="ms",
             sub="Connect message → ack", tone="accent"),
        dict(label="Subscribe acknowledged", value=C.fmt_ms(summary.get("subscribe_ack_ms")), unit="ms",
             sub="Subscribe → ack (if identifiable)", tone="accent"),
        dict(label="First market message", value=C.fmt_ms(summary.get("first_message_ms")), unit="ms",
             sub="Subscribe → first update", tone="accent"),
    ])
    st.write("")
    counts = summary.get("message_counts") or {}
    C.card_row([
        dict(label="Messages received", value=C.fmt_int(summary.get("total_messages",
                                                                    sum(counts.values()))),
             sub=" · ".join(f"{k}: {v}" for k, v in counts.items()) or None),
        dict(label="Arrival intervals", value=C.fmt_int(len(intervals)),
             sub=f"Long pauses: {C.fmt_int(summary.get('long_pauses'))}"),
        dict(label="Disconnects / reconnects",
             value=f"{C.fmt_int(summary.get('disconnects'))} / {C.fmt_int(summary.get('reconnects'))}",
             tone="failure" if summary.get("disconnects") else "neutral"),
        dict(label="Errors", value=C.fmt_int(summary.get("error_count", 0)),
             tone="failure" if summary.get("error_count") else "neutral"),
    ])
    for w in summary.get("warnings") or []:
        st.warning(w, icon=":material/warning:")
    if intervals:
        st.markdown("#### Message arrival intervals")
        C.show_chart(C.interval_histogram(intervals, _stat(intervals)), key=f"{key}_ia")
        C.show_sample_note(len(intervals))
        st.caption("P50/P95/P99 here describe gaps between arriving messages (numpy linear method), "
                   "not latency.")
    if summary.get("errors"):
        with st.expander("Errors (redacted)"):
            st.code("\n".join(map(str, summary["errors"])), language="text")


tab_run, tab_hist = st.tabs(["Run", "History"])

with tab_run:
    token = active_access_token()
    ready = bool(token and settings.client_id)
    if not ready:
        st.error("**WebSocket tests need an access token and a client ID.** Add `TS_CLIENT_ID` to "
                 "`.env`, and get a token on **Settings → Get an access token** (or set "
                 "`TS_ACCESS_TOKEN`).", icon=":material/key_off:")
        if st.button("Open Settings", icon=":material/settings:"):
            nav.go_to(nav.PAGE_SETTINGS)
    with st.form("ws_form"):
        w1, w2, w3 = st.columns([2, 1, 1])
        instruments = w1.text_input("Instruments", value="NSE|22",
                                    help="Exchange|token pairs, separated by # or commas. "
                                         "Example: NSE|22#NSE|2885")
        feed = w2.radio("Feed", ["t", "d"], horizontal=True,
                        format_func={"t": "Touchline", "d": "Depth"}.get)
        duration = w3.number_input("Duration (s)", 5, 300, 30, step=5)
        submitted = st.form_submit_button("Start WebSocket test", type="primary",
                                          icon=":material/play_arrow:", disabled=not ready)
    st.caption("Outside market hours (09:15–15:30 IST) you will see connect and ack times but few "
               "or no feed messages.")
    if submitted and ready:
        items = [s.strip() for s in instruments.replace(",", "#").split("#") if s.strip()]
        if not items:
            st.error("Enter at least one instrument, like NSE|22.", icon=":material/error:")
        else:
            from src.websocket_benchmark import WSBenchmarkConfig, run_ws_benchmark_sync

            cfg = WSBenchmarkConfig(ws_url=settings.ws_url, client_id=settings.client_id,
                                    access_token=token, instruments=items, feed=feed,
                                    duration_s=float(duration))
            try:
                with st.spinner(f"Listening for {duration}s…"):
                    res = run_ws_benchmark_sync(cfg, db, environment=settings.environment)
                st.session_state["ws_last"] = res.run_id
                st.success(f"Finished run `{res.run_id}`.", icon=":material/check_circle:")
                render_ws_result(res.summary(), list(res.inter_arrival_ms), key="ws_now")
            except Exception as exc:
                try:
                    from src.redaction import redact_text

                    msg = redact_text(str(exc), [token, settings.client_id])
                except Exception:
                    msg = type(exc).__name__
                st.error(f"The WebSocket test couldn't run: {msg}. Check the instruments and your "
                         "token, then try again.", icon=":material/error:")

with tab_hist:
    try:
        ws_runs = db.list_runs(include_synthetic=True, run_types=["websocket"])
    except Exception:
        ws_runs = pd.DataFrame()
    if ws_runs is None or not len(ws_runs):
        C.empty_state("No WebSocket runs yet",
                      "Run a short test on the **Run** tab — 30 seconds on one liquid instrument "
                      "during market hours is a good start.", icon=":material/cell_tower:")
    else:
        by_id = {r["run_id"]: r for r in ws_runs.to_dict("records")}
        ids = list(by_id)
        default = ids.index(st.session_state["ws_last"]) if st.session_state.get("ws_last") in ids else 0
        rid = st.selectbox("WebSocket run", ids, index=default,
                           format_func=lambda r: C.run_label(by_id[r]))
        C.run_meta_block(db.get_run(rid))
        samples = db.get_ws_samples(rid)
        if not len(samples):
            st.info("This run stored no samples.", icon=":material/info:")
        else:
            def first(metric: str):
                v = samples.loc[samples["metric"] == metric, "value"]
                return float(v.iloc[0]) if len(v) else None

            intervals = samples.loc[samples["metric"] == "inter_arrival_ms", "value"].astype(float).tolist()
            detail_counts = samples.loc[samples["metric"] == "message_count"]
            summary = {
                "connect_ms": first("connect_ms"), "auth_ack_ms": first("auth_ack_ms"),
                "subscribe_ack_ms": first("subscribe_ack_ms"),
                "first_message_ms": first("first_message_ms"),
                "disconnects": int(samples.loc[samples["metric"] == "disconnect", "value"].sum()),
                "reconnects": int(samples.loc[samples["metric"] == "reconnect", "value"].sum()),
                "message_counts": ({str(d): int(v) for d, v in zip(detail_counts["detail"],
                                                                   detail_counts["value"])}
                                   if len(detail_counts) else {}),
                "total_messages": (int(detail_counts["value"].sum()) if len(detail_counts)
                                   else len(intervals) + (1 if intervals else 0)),
                "long_pauses": (int(np.sum(np.asarray(intervals) >
                                           max(1000.0, 10 * float(np.median(intervals)))))
                                if intervals else None),
            }
            render_ws_result(summary, intervals, key=f"ws_hist_{rid}")
        st.caption(f"{len(ws_runs)} WebSocket run(s) stored. Delete runs on the Settings page.")
