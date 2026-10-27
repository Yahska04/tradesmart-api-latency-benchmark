"""Guarded LIVE order-latency test: PlaceOrder + immediate CancelOrder, one tiny far-from-market order at a time."""
from __future__ import annotations

import sys
import threading
from datetime import datetime, time as dtime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from src.ui import components as C  # noqa: E402
from src.ui import nav  # noqa: E402
from src.ui.state import (  # noqa: E402
    active_access_token, all_secrets, endpoints_by_id, get_db, get_logger, get_settings, set_selection,
)
from src.ui.theme import apply_theme  # noqa: E402

apply_theme("Order latency test",
            subtitle="Time PlaceOrder and CancelOrder on your live account, inside tight guardrails.")

from src.order_test import (  # noqa: E402
    ALLOWED_EXCHANGES, ALLOWED_PRODUCTS, DEFAULT_OFFSET_PCT, IST, MAX_OFFSET_PCT, MAX_ORDER_VALUE_INR,
    MAX_ORDERS_PER_DAY, MIN_OFFSET_PCT, ORDER_QTY, REQUIRED_ENDPOINTS, OrderSpec, OrderTestError,
    DEFAULT_BURST_SIZE, MAX_BURST_SIZE, MAX_ORDER_OPS_PER_SECOND, MIN_BURST_PAUSE_S, ORDER_TEST_SERIES,
    DEFAULT_BURST_PAUSE_S, MAX_BURST_PAUSE_S, ORDER_RATE_WINDOW_S,
    confirmation_phrase, orders_placed_today, parse_quote, run_order_test, safe_limit_price,
)

PREVIEW_KEY, RESULT_KEY = "order_test_preview", "order_test_result"
JOB_KEY, ERROR_KEY = "order_test_job", "order_test_error"

settings = get_settings()
db = get_db()
eps = endpoints_by_id()

st.error(
    "**This page places REAL orders on your TradeSmart account.** Each order is a BUY LIMIT for "
    f"{ORDER_QTY} share, priced well below the market and cancelled within about 1–2 seconds. A fill is very unlikely, "
    "but not impossible in a sharp fall. Use it only if you accept that.", icon=":material/warning:")

with st.expander("Guardrails on every order", icon=":material/shield:"):
    st.markdown(
        f"- BUY · LIMIT · DAY · quantity {ORDER_QTY} · {' / '.join(ALLOWED_EXCHANGES)} cash equities only — "
        "never a market order\n"
        f"- Limit price {MIN_OFFSET_PCT:g}–{MAX_OFFSET_PCT:g}% below the live price (fresh quote at the start of every "
        "burst), rounded down to the tick, never below the lower circuit\n"
        f"- Order value at most ₹{MAX_ORDER_VALUE_INR:,}\n"
        f"- At most {MAX_ORDERS_PER_DAY} orders per trading day (IST), counted from saved results\n"
        "- You type a confirmation for every batch\n"
        f"- One input runs the whole batch: bursts of up to {MAX_BURST_SIZE} orders placed back to back, then "
        "every order of the burst is cancelled and its final status read back, then your chosen delay before "
        "the next burst\n"
        f"- Placements and cancellations held to {MAX_ORDER_OPS_PER_SECOND} per rolling "
        f"{ORDER_RATE_WINDOW_S:g} s (TradeSmart allows 10 per second) · a fresh quote starts every burst · "
        "no automatic retries\n"
        "- The batch stops after the current burst on the first problem (every order of that burst is still "
        "cancelled) and tells you which order to check · you can stop a running batch")

missing_eps = [e for e in REQUIRED_ENDPOINTS if e not in eps]
if missing_eps:
    st.error(f"The endpoint registry is missing {', '.join(missing_eps)}. Restore config/endpoints.json, "
             "then reload.", icon=":material/error:")
    st.stop()

token = active_access_token()
problems_global = []
if not token:
    problems_global.append("add an access token (Settings → Get an access token)")
if not settings.client_id:
    problems_global.append("set TS_CLIENT_ID in .env")

used = orders_placed_today(db)
remaining = max(0, MAX_ORDERS_PER_DAY - used)
now_ist = datetime.now(timezone.utc).astimezone(IST)
market_open = now_ist.weekday() < 5 and dtime(9, 15) <= now_ist.time() <= dtime(15, 30)

c1, c2, c3 = st.columns(3)
with c1:
    C.metric_card("Orders left today", f"{remaining} / {MAX_ORDERS_PER_DAY}", sub=f"{used} used (IST day)",
                  tone="failure" if remaining == 0 else "accent")
with c2:
    C.metric_card("Market", "Open" if market_open else "Closed", sub=now_ist.strftime("%a %d %b, %H:%M IST"),
                  tone="success" if market_open else "neutral")
with c3:
    C.metric_card("Environment", settings.environment.upper(), sub="Real account" if settings.is_live() else
                  "Non-live environment", tone="failure" if settings.is_live() else "neutral")
if not market_open:
    st.warning("The market is closed. TradeSmart will probably reject orders now — a rejection still gives a "
               "latency sample, but not of the normal in-market path.", icon=":material/schedule:")
if problems_global:
    st.error("Before you can test: " + "; ".join(problems_global) + ".", icon=":material/key_off:")
    nav.nav_link(nav.PAGE_SETTINGS, "Open Settings", ":material/settings:")

# ============================================================================ 1 · instrument
st.subheader("1 · Choose the instrument")
from src.ui.instrument_picker import instrument_picker  # noqa: E402

exch, tsym, tok = ALLOWED_EXCHANGES[0], "", ""
manual = st.toggle("Enter symbol and token manually", value=False, key="order_manual",
                   help="Use this if the script master isn't downloaded yet.")
if not manual:
    inst = instrument_picker("order_instr", exchanges=list(ALLOWED_EXCHANGES), label="Share",
                             help="Type a symbol or company name, e.g. SBIN. NSE and BSE cash equities only.")
    if inst is not None:
        if inst.instrument.upper() in ORDER_TEST_SERIES.get(inst.exch, set()):
            exch, tsym, tok = inst.exch, inst.tsym, inst.token
            st.caption(f"{inst.exch} · {inst.tsym} · token {inst.token} · series {inst.instrument} · "
                       f"tick ₹{inst.tick_size:g}")
        else:
            st.warning(f"{inst.tsym} is series **{inst.instrument}**, not a regular cash-equity share "
                       f"(allowed: NSE {', '.join(sorted(ORDER_TEST_SERIES['NSE']))}; BSE groups "
                       f"{', '.join(sorted(ORDER_TEST_SERIES['BSE']))}). Pick another instrument.",
                       icon=":material/block:")
else:
    i1, i2, i3 = st.columns([1, 2, 2])
    exch = i1.selectbox("Exchange", ALLOWED_EXCHANGES)
    tsym = i2.text_input("Trading symbol", placeholder="e.g. ABC-EQ",
                         help="Exactly as TradeSmart writes it, e.g. with the -EQ suffix on NSE.")
    tok = i3.text_input("Instrument token", placeholder="numeric, e.g. 12345",
                        help="From the script master or SearchScrip. The quote must return the same symbol, "
                             "or nothing is sent.")
p1, p2 = st.columns(2)
product = p1.selectbox("Product", list(ALLOWED_PRODUCTS), format_func=lambda k: f"{k} — {ALLOWED_PRODUCTS[k]}")
offset = p2.slider("Price below market (%)", MIN_OFFSET_PCT, MAX_OFFSET_PCT, DEFAULT_OFFSET_PCT, step=0.5,
                   help="Further below = even less chance of a fill. Never closer than "
                        f"{MIN_OFFSET_PCT:g}%. Clamped to the lower circuit.")
spec = OrderSpec(exch=exch, tsym=tsym.strip(), token=tok.strip(), product=product, offset_pct=float(offset))
spec_key = (spec.exch, spec.tsym.upper(), spec.token, spec.product, spec.offset_pct)
spec_problems = spec.problems()

preview = st.session_state.get(PREVIEW_KEY)
if preview and preview["key"] != spec_key:
    preview = None
    st.session_state.pop(PREVIEW_KEY, None)

if st.button("Get quote and preview", icon=":material/request_quote:",
             disabled=bool(spec_problems or problems_global),
             help="Sends one read-only GetQuotes request. No order is placed."):
    from src.api_client import TradeSmartClient

    try:
        with TradeSmartClient(settings.base_url, access_token=token, secrets=all_secrets()) as client:
            res = client.timed_post(eps["GetQuotes"], {"uid": settings.client_id, "exch": spec.exch,
                                                      "token": spec.token}, run_id="preview", seq=1,
                                    is_warmup=True)
            if not res.success:
                raise OrderTestError(f"the quote request failed ({res.error_message or res.outcome})")
            q = parse_quote(client.last_response_body)
        if q.tsym and q.tsym.upper() != spec.tsym.upper():
            raise OrderTestError(f"token {spec.token} is {q.tsym}, not {spec.tsym}. Fix the symbol or token.")
        price = safe_limit_price(q, spec.offset_pct)
        if price * ORDER_QTY > MAX_ORDER_VALUE_INR:
            raise OrderTestError(f"order value ₹{price * ORDER_QTY} is above the ₹{MAX_ORDER_VALUE_INR} cap. "
                                 "Pick a lower-priced instrument.")
        preview = {"key": spec_key, "tsym": q.tsym or spec.tsym, "ltp": q.ltp, "price": price,
                   "lc": q.lower_circuit, "fetched": datetime.now(timezone.utc)}
        st.session_state[PREVIEW_KEY] = preview
    except OrderTestError as exc:
        st.error(f"Can't prepare this order: {exc}", icon=":material/block:")
    except Exception as exc:
        st.error(f"The quote couldn't be fetched: {exc}. Check your connection and token, then try again.",
                 icon=":material/error:")
elif spec_problems and (tsym or tok):
    st.caption("To preview: " + "; ".join(spec_problems) + ".")

if preview:
    v1, v2, v3, v4 = st.columns(4)
    with v1:
        C.metric_card("Symbol", preview["tsym"], sub=f"{spec.exch} · token {spec.token}")
    with v2:
        C.metric_card("Live price", f"₹{preview['ltp']}", sub="from GetQuotes")
    with v3:
        C.metric_card("Your limit price", f"₹{preview['price']}", sub=f"{spec.offset_pct:g}% below · BUY {ORDER_QTY}",
                      tone="accent")
    with v4:
        C.metric_card("Lower circuit", f"₹{preview['lc']}" if preview["lc"] else C.DASH,
                      sub=f"Order value ₹{preview['price'] * ORDER_QTY}")
    st.caption("Indicative only: the price is recalculated from a fresh quote at the start of every burst.")

# ============================================================================ 2 · place
def _attempt_rows(attempts) -> pd.DataFrame:
    return pd.DataFrame([{
        "#": a.seq,
        "Order no.": a.norenordno or C.DASH,
        "Live price (₹)": f"{a.ltp:.2f}" if a.ltp is not None else C.DASH,
        "Limit price (₹)": f"{a.price:.2f}" if a.price is not None else C.DASH,
        "PlaceOrder (ms)": round(a.place.latency_ms, 1) if a.place and a.place.latency_ms is not None else None,
        "Place result": (a.place.outcome if a.place else "not sent"),
        "CancelOrder (ms)": round(a.cancel.latency_ms, 1) if a.cancel and a.cancel.latency_ms is not None else None,
        "Cancel result": (a.cancel.outcome if a.cancel else C.DASH),
        "Final status": a.final_status or C.DASH,
    } for a in attempts])


def _worker(job: dict, client_kwargs: dict, run_kwargs: dict) -> None:
    """Background thread: runs the batch. Touches only the plain ``job`` dict, never Streamlit APIs."""
    from src.api_client import TradeSmartClient

    try:
        with TradeSmartClient(**client_kwargs) as client:
            job["outcome"] = run_order_test(db, eps, client, stop_event=job["stop"],
                                            on_attempt=lambda a: job["attempts"].__setitem__(a.seq, a),
                                            **run_kwargs)
    except OrderTestError as exc:
        job["error"] = f"Nothing was sent: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        job["error"] = f"The order test stopped unexpectedly: {exc}. Check your order book in TradeSmart."
    finally:
        job["done"] = True


job = st.session_state.get(JOB_KEY)


@st.fragment(run_every=1.0)
def _progress_panel() -> None:
    job = st.session_state.get(JOB_KEY)
    if not job:
        return
    if job.get("done"):
        st.session_state.pop(JOB_KEY, None)
        if job.get("error"):
            st.session_state[ERROR_KEY] = job["error"]
        if job.get("outcome") is not None:
            st.session_state[RESULT_KEY] = job["outcome"]
        st.session_state["order_batches"] = st.session_state.get("order_batches", 0) + 1
        st.rerun(scope="app")
    attempts = [job["attempts"][k] for k in sorted(job["attempts"])]
    placed = len(attempts)
    done = sum(1 for a in attempts if a.cancel is not None or a.alert)
    burst_no = -(-placed // job["burst"]) if placed else 0
    st.progress(min(1.0, done / job["count"]),
                text=f"Burst {burst_no} of {job['bursts']} · {placed} placed · {done} of {job['count']} "
                     "cancelled and confirmed…")
    if job["stop"].is_set():
        st.caption("Stopping — orders already placed in this burst are being cancelled…")
    elif st.button("Stop the batch", icon=":material/stop_circle:", key="order_stop",
                   help="No new orders are placed; orders already placed in this burst are still cancelled."):
        job["stop"].set()
    if attempts:
        st.dataframe(_attempt_rows(attempts[-10:]), hide_index=True, width="stretch")


st.subheader("2 · Place and cancel")
if job:
    st.info("An order batch is running. Keep this tab open — you can stop it between orders.",
            icon=":material/sync:")
    _progress_panel()
elif remaining == 0:
    st.info(f"You've used all {MAX_ORDERS_PER_DAY} test orders for today. The limit resets at midnight IST.",
            icon=":material/event_busy:")
else:
    o1, o2, o3, o4 = st.columns(4)
    count = int(o1.number_input("Total orders", 1, remaining, min(100, remaining),
                                help=f"The whole batch runs from this one input. Up to {MAX_ORDERS_PER_DAY} per "
                                     f"trading day (IST); {remaining} left today."))
    burst = int(o2.number_input("Orders per burst", 1, MAX_BURST_SIZE, DEFAULT_BURST_SIZE,
                                help=f"Placed back to back, within about one second. At most {MAX_BURST_SIZE} "
                                     "(TradeSmart allows 10 orders per second)."))
    pause = float(o3.number_input("Delay between bursts (s)", MIN_BURST_PAUSE_S, MAX_BURST_PAUSE_S,
                                  DEFAULT_BURST_PAUSE_S, step=0.5,
                                  help="Time from the last order of one burst to the first order of the next. "
                                       "Cancellations and status checks happen inside this delay."))
    reuse = o4.toggle("Reuse connections (warm)", value=True,
                      help="Off = new connection per request (cold): each sample includes DNS, TCP and TLS.")
    bursts = -(-count // burst)
    est_s = (bursts - 1) * pause + bursts * 1.5   # delay + ~1.5 s to place, cancel and confirm a burst
    est = f"{est_s / 60:.1f} min" if est_s >= 90 else f"{est_s:.0f} s"
    st.caption(f"**{count} orders → {bursts} burst{'s' if bursts != 1 else ''} of up to {burst}, "
               f"{pause:g} s apart · about {est}.** Each burst: place {burst} orders → cancel them all → "
               f"confirm → wait. Placements + cancellations stay at or under {MAX_ORDER_OPS_PER_SECOND} per "
               f"{ORDER_RATE_WINDOW_S:g} s. Orders stay open for roughly 1–2 s before they are cancelled.")
    phrase = confirmation_phrase(count)
    # A fresh key per batch: the confirmation never carries over to the next batch.
    typed = st.text_input(f'Type **{phrase}** to confirm', autocomplete="off",
                          key=f"order_confirm_{st.session_state.get('order_batches', 0)}")
    ready = bool(preview) and not problems_global and typed.strip() == phrase
    if st.button(f"Place and cancel {count} order{'s' if count != 1 else ''}", type="primary",
                 icon=":material/bolt:", disabled=not ready):
        from src.database import new_run_id

        get_logger()
        for key in (RESULT_KEY, ERROR_KEY):
            st.session_state.pop(key, None)
        new_job = {"stop": threading.Event(), "attempts": {}, "count": count, "burst": burst, "bursts": bursts,
                   "done": False,
                   "outcome": None, "error": None}
        client_kwargs = dict(base_url=settings.base_url, access_token=token, timeout_s=10.0,
                             connection_mode="warm" if reuse else "cold", secrets=all_secrets())
        run_kwargs = dict(client_id=settings.client_id, spec=spec, count=count, confirmation=typed,
                          run_id=new_run_id("order"), environment=settings.environment,
                          base_url=settings.base_url, timeout_s=10.0, burst_size=burst, burst_pause_s=pause)
        threading.Thread(target=_worker, args=(new_job, client_kwargs, run_kwargs), daemon=True,
                         name="order-latency-test").start()
        st.session_state[JOB_KEY] = new_job
        st.rerun()
    elif not ready:
        reasons = []
        if problems_global:
            reasons.append("fix the items above")
        if not preview:
            reasons.append("get a quote and preview")
        if typed.strip() != phrase:
            reasons.append(f'type "{phrase}"')
        st.caption("To place: " + ", ".join(reasons) + ".")

# ============================================================================ results
if st.session_state.get(ERROR_KEY):
    st.error(st.session_state[ERROR_KEY], icon=":material/block:")
outcome = st.session_state.get(RESULT_KEY)
if not outcome or job:
    st.stop()

st.subheader("Results")
for alert in outcome.alerts:
    st.error(alert, icon=":material/report:")
if outcome.stop_reason and not outcome.alerts:
    st.warning(outcome.stop_reason, icon=":material/pause_circle:")

placed = [a.place.latency_ms for a in outcome.attempts if a.place is not None and a.place.success]
cancelled = [a.cancel.latency_ms for a in outcome.attempts if a.cancel is not None and a.cancel.success]
r1, r2, r3, r4 = st.columns(4)
with r1:
    C.metric_card("PlaceOrder median", C.fmt_ms(pd.Series(placed).median() if placed else None), "ms",
                  sub=f"{len(placed)} accepted", tone="accent")
with r2:
    C.metric_card("PlaceOrder P95", C.fmt_ms(pd.Series(placed).quantile(0.95) if len(placed) >= 2 else None), "ms",
                  sub="indicative below 20 samples" if len(placed) < 20 else f"{len(placed)} samples",
                  sub_warn=len(placed) < 20)
with r3:
    C.metric_card("CancelOrder median", C.fmt_ms(pd.Series(cancelled).median() if cancelled else None), "ms",
                  sub=f"{len(cancelled)} acknowledged", tone="accent")
with r4:
    safe = sum(1 for a in outcome.attempts if a.final_status in ("CANCELED", "CANCELLED", "REJECTED"))
    C.metric_card("Confirmed closed", f"{safe} / {len(outcome.attempts)}", sub="CANCELED or REJECTED",
                  tone="success" if safe == len(outcome.attempts) and not outcome.alerts else "failure")
st.caption("Client-observed latency to TradeSmart's order system (OMS acknowledgement), not exchange "
           "execution time. Full percentiles for this run are in Performance analytics.")

st.dataframe(_attempt_rows(outcome.attempts), hide_index=True, width="stretch")

st.success(f"Saved to history as run `{outcome.run_id}`.", icon=":material/check_circle:")
if st.button("Open in Performance analytics", icon=":material/monitoring:"):
    set_selection(run_id=outcome.run_id, endpoint_id="PlaceOrder")
    nav.go_to(nav.PAGE_ANALYTICS)
