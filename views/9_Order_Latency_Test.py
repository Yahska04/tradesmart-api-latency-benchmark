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
    active_access_token, all_secrets, clear_egress_status, current_identity, egress_label, egress_status,
    endpoints_by_id, get_db, get_logger, get_settings, set_selection,
)
from src.ui.theme import apply_theme  # noqa: E402

apply_theme("Order latency test",
            subtitle="Time PlaceOrder and CancelOrder on your live account, inside tight guardrails.")

identity = current_identity()
if not identity.can_place_orders:
    st.warning("Order tests are limited to the people on the admin's order access list. Ask an admin if you "
               "need access.", icon=":material/lock:")
    st.stop()

from src import network_guard, run_lock  # noqa: E402
from src.order_test import (  # noqa: E402
    NEEDS_CHECK_SETTING,
    ALLOWED_EXCHANGES, ALLOWED_PRODUCTS, DEFAULT_OFFSET_PCT, IST, MAX_OFFSET_PCT, MAX_ORDER_VALUE_INR,
    MAX_ORDERS_PER_DAY, MIN_OFFSET_PCT, ORDER_QTY, REQUIRED_ENDPOINTS, OrderSpec, OrderTestError,
    DEFAULT_BURST_SIZE, MAX_BURST_SIZE, MAX_ORDER_OPS_PER_SECOND, MIN_BURST_PAUSE_S, ORDER_TEST_SERIES,
    DEFAULT_BURST_PAUSE_S, MAX_BURST_PAUSE_S, ORDER_RATE_WINDOW_S,
    confirmation_phrase, orders_placed_today, parse_quote, reconcile_test_orders, run_order_test,
    safe_limit_price,
)
from src.redaction import redact_text  # noqa: E402

PREVIEW_KEY, RECONCILE_KEY = "order_test_preview", "order_test_reconcile"


@st.cache_resource(show_spinner=False)
def _job_store() -> dict:
    """The one running batch and the last result, shared by every user of this server process."""
    return {"job": None, "last": None, "lock": threading.Lock()}


store = _job_store()
settings = get_settings()
db = get_db()
eps = endpoints_by_id()

st.error(
    f"**This page places REAL orders on {'the shared' if settings.is_server() else 'your'} TradeSmart account.** "
    f"Each order is a BUY LIMIT for "
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
job = store["job"]

used = orders_placed_today(db)
remaining = max(0, MAX_ORDERS_PER_DAY - used)
now_ist = datetime.now(timezone.utc).astimezone(IST)
market_open = now_ist.weekday() < 5 and dtime(9, 15) <= now_ist.time() <= dtime(15, 30)

check = egress_status()
ip_block = check.order_block_reason
c1, c2, c3, c4 = st.columns(4)
with c1:
    C.metric_card("Orders left today", f"{remaining} / {MAX_ORDERS_PER_DAY}", sub=f"{used} used (IST day)",
                  tone="failure" if remaining == 0 else "accent")
with c2:
    C.metric_card("Market", "Open" if market_open else "Closed", sub=now_ist.strftime("%a %d %b, %H:%M IST"),
                  tone="success" if market_open else "neutral")
with c3:
    C.metric_card("Environment", settings.environment.upper(), sub="Real account" if settings.is_live() else
                  "Non-live environment", tone="failure" if settings.is_live() else "neutral")
with c4:
    ip_label, ip_kind = egress_label(check)
    C.metric_card("Outbound IP", check.detected or "Unknown",
                  sub=f"{ip_label} · static IP {check.expected or 'not set'}",
                  tone={"success": "success", "failure": "failure"}.get(ip_kind, "failure" if ip_block else "neutral"))
if ip_block:
    st.error(f"Orders are blocked. {ip_block}", icon=":material/public_off:")
    b1, b2, _ = st.columns([1, 1, 3])
    with b1:
        nav.nav_link(nav.PAGE_SETTINGS, "Open Settings", ":material/settings:")
    if b2.button("Check the IP again", icon=":material/refresh:"):
        clear_egress_status()
        st.rerun()
elif check.problem and settings.is_server():
    st.warning(check.problem, icon=":material/public:")
if not market_open:
    st.warning("The market is closed. TradeSmart will probably reject orders now — a rejection still gives a "
               "latency sample, but not of the normal in-market path.", icon=":material/schedule:")
if problems_global:
    st.error("Before you can test: " + "; ".join(problems_global) + ".", icon=":material/key_off:")
    nav.nav_link(nav.PAGE_SETTINGS, "Open Settings", ":material/settings:")

other_run = None if job else run_lock.current_run(run_lock.lock_path_for(db.path))
if other_run:
    st.warning(run_lock.describe(other_run), icon=":material/hourglass_top:")

# ============================================================================ open test orders
# A running batch's own orders are open by design, so they only count once nothing is running.
unresolved = [] if job or other_run else db.unresolved_test_orders()
needs_check = not job and not other_run and bool(db.get_setting(NEEDS_CHECK_SETTING))
if needs_check:
    st.error("**An earlier order got no reply from TradeSmart, so it may exist.** New batches stay blocked until "
             "you press **Check for open test orders** below. Also confirm in your TradeSmart order book.",
             icon=":material/report:")
if unresolved:
    st.error(f"**{len(unresolved)} test order{'s are' if len(unresolved) != 1 else ' is'} not confirmed closed.** "
             "New batches stay blocked until they are checked. Press **Check for open test orders** below.",
             icon=":material/report:")


def _show_reconcile(report) -> None:
    st.markdown(f"Checked {report.checked} · cancelled {len(report.cancelled)} · already closed "
                f"{len(report.closed)} · filled {len(report.filled)} · still open {len(report.still_open)} · "
                f"errors {len(report.errors)}")
    if report.filled:
        st.error(f"Filled, so you now hold these shares: {', '.join(report.filled)}. Review them in TradeSmart.",
                 icon=":material/report:")
    if report.still_open:
        st.error(f"Still open: {', '.join(report.still_open)}. Cancel them in TradeSmart, then check again.",
                 icon=":material/report:")
    for problem in report.errors:
        st.error(problem, icon=":material/error:")
    if not report.needs_attention:
        st.success("No test orders are left open.", icon=":material/check_circle:")


with st.expander("Open test orders", expanded=bool(unresolved or needs_check), icon=":material/search_check:"):
    st.caption("Finds test orders left open, for example after a crash, and cancels them. It only ever touches "
               "this app's own tagged test orders, never your other orders.")
    no_order_book = "OrderBook" not in eps
    if no_order_book:
        st.caption("The endpoint registry is missing OrderBook. Restore config/endpoints.json to use this.")
    if st.button("Check for open test orders", icon=":material/search_check:",
                 disabled=bool(problems_global or job or other_run or no_order_book),
                 help="Reads the order book and order status, and cancels this app's own open test orders."):
        from src.api_client import TradeSmartClient

        get_logger()
        try:
            with st.spinner("Checking…"), TradeSmartClient(settings.base_url, access_token=token, timeout_s=10.0,
                                                           secrets=all_secrets()) as client:
                st.session_state[RECONCILE_KEY] = reconcile_test_orders(
                    db, eps, client, client_id=settings.client_id, started_by=identity.username)
        except OrderTestError as exc:
            st.session_state[RECONCILE_KEY] = str(exc)
        except Exception as exc:
            st.session_state[RECONCILE_KEY] = redact_text(
                f"The check stopped unexpectedly: {exc}. Check your order book in TradeSmart.", all_secrets())
        st.rerun()
    reconcile = st.session_state.get(RECONCILE_KEY)
    if isinstance(reconcile, str):
        st.error(reconcile, icon=":material/error:")
    elif reconcile is not None:
        _show_reconcile(reconcile)

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


def _worker(store: dict, job: dict, db, eps: dict, client_kwargs: dict, run_kwargs: dict) -> None:
    """Background thread: runs the batch. Touches only plain dicts, never Streamlit APIs."""
    from src.api_client import TradeSmartClient

    outcome, error = None, None
    try:
        with TradeSmartClient(**client_kwargs) as client:
            outcome = run_order_test(db, eps, client, stop_event=job["stop"],
                                     on_attempt=lambda a: job["attempts"].__setitem__(a.seq, a), **run_kwargs)
    except OrderTestError as exc:
        error = f"Nothing was sent: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        error = redact_text(f"The order test stopped unexpectedly: {exc}. Check your order book in TradeSmart.",
                            client_kwargs["secrets"])
    finally:
        with store["lock"]:
            store["last"] = {"outcome": outcome, "error": error, "started_by": job["started_by"]}
            store["job"] = None


@st.fragment(run_every=1.0)
def _progress_panel() -> None:
    job = store["job"]
    if not job:
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
    st.info(f"An order batch started by {job['started_by']} is running. It carries on if you close this tab. "
            "Anyone with order access can stop it.", icon=":material/sync:")
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
    typed = st.text_input(f'Type **{phrase}** to confirm', autocomplete="off",
                          key=f"order_confirm_{st.session_state.get('order_batches', 0)}")
    ready = (bool(preview) and not problems_global and typed.strip() == phrase and not ip_block and not other_run
             and not unresolved and not needs_check)
    if st.button(f"Place and cancel {count} order{'s' if count != 1 else ''}", type="primary",
                 icon=":material/bolt:", disabled=not ready):
        from src.database import new_run_id

        get_logger()
        new_job = {"stop": threading.Event(), "attempts": {}, "count": count, "burst": burst, "bursts": bursts,
                   "started_by": identity.username}
        client_kwargs = dict(base_url=settings.base_url, access_token=token, timeout_s=10.0,
                             connection_mode="warm" if reuse else "cold", secrets=all_secrets())
        run_kwargs = dict(client_id=settings.client_id, spec=spec, count=count, confirmation=typed,
                          run_id=new_run_id("order"), environment=settings.environment,
                          base_url=settings.base_url, timeout_s=10.0, burst_size=burst, burst_pause_s=pause,
                          started_by=identity.username, ip_guard=network_guard.order_guard(settings, db))
        with store["lock"]:
            if store["job"] is None:  # another user may have started a batch since this page was drawn
                thread = threading.Thread(target=_worker, args=(store, new_job, db, eps, client_kwargs, run_kwargs),
                                          daemon=True, name="order-latency-test")
                thread.start()
                store["job"], store["last"] = new_job, None
        # A fresh key: the confirmation never carries over to the next batch.
        st.session_state["order_batches"] = st.session_state.get("order_batches", 0) + 1
        st.rerun()
    elif not ready:
        reasons = []
        if problems_global:
            reasons.append("fix the items above")
        if ip_block:
            reasons.append("fix the server IP problem above")
        if other_run:
            reasons.append("wait for the other run to finish")
        if unresolved or needs_check:
            reasons.append("check the open test orders above")
        if not preview:
            reasons.append("get a quote and preview")
        if typed.strip() != phrase:
            reasons.append(f'type "{phrase}"')
        st.caption("To place: " + ", ".join(reasons) + ".")

# ============================================================================ results
last = store["last"]
if job or not last:
    st.stop()
if last["error"]:
    st.error(f"Batch started by {last['started_by']}. {last['error']}", icon=":material/block:")
outcome = last["outcome"]
if not outcome:
    st.stop()

st.subheader("Results")
st.caption(f"Last batch, started by {outcome.started_by}.")
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
