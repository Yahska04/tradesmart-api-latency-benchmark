"""Guarded LIVE order-latency test: place tiny far-from-market LIMIT orders in bursts and cancel them all.

This is the ONLY code path in the app that sends ``PlaceOrder`` / ``CancelOrder``. It exists because the
account owner explicitly opted in to measuring order latency on their live account (2026-10-19: "Live,
guarded", auto-cancel; 2026-10-22: bursts of 8; 2026-10-26: up to 1000 orders per trading day, one input
for the whole run, configurable delay between bursts). The generic benchmark runner and the "Check an
endpoint" page still refuse every state-changing endpoint via :mod:`src.safety`.

Guardrails (all enforced here, before anything is sent):

* BUY, LIMIT, DAY, quantity 1, NSE/BSE cash equities only, products CNC or MIS — never a market order.
* Limit price = live price minus an offset (>= 1 %, default 3 %), rounded DOWN to the tick size, never below
  the lower circuit. A fresh quote is fetched at the start of every burst and its symbol must match the one
  entered.
* Order value <= ``MAX_ORDER_VALUE_INR``.
* At most ``MAX_ORDERS_PER_DAY`` PlaceOrder attempts per IST trading day, counted from stored results.
* The caller must pass the exact typed confirmation ``PLACE <n> ORDERS`` (once for the whole run).
* Each burst places up to ``MAX_BURST_SIZE`` orders back to back, then cancels every accepted order of that
  burst (even if one cancel fails) and reads each final status back with ``SingleOrdHist``. The batch stops
  after the burst on the first problem (rejected placement, no reply, failed cancel, fill, unexpected status).
* The next burst starts ``burst_pause_s`` (1–60 s, default 3) after the previous burst's last order, and an
  :class:`OrderRateGate` keeps PlaceOrder + CancelOrder at most ``MAX_ORDER_OPS_PER_SECOND`` per rolling
  ``ORDER_RATE_WINDOW_S`` (TradeSmart's limit is 10 orders/s). Nothing is ever retried.

PlaceOrder and CancelOrder samples are saved as a normal measured run (``config.source = "order_test"``).
"""
from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote, unquote, urlparse

import pandas as pd

from src import run_lock
from src.metrics import endpoint_metrics

if TYPE_CHECKING:
    from src.api_client import RequestResult, TradeSmartClient
    from src.database import Database
    from src.endpoint_registry import Endpoint

logger = logging.getLogger("tsbench")

MAX_ORDERS_PER_DAY = 1000
MAX_ORDER_VALUE_INR = Decimal("5000")
ORDER_QTY = 1
ALLOWED_EXCHANGES = ("NSE", "BSE")
# Script-master series/groups that are regular cash-equity shares (excludes bonds, T-bills, ETFs-as-MF, BSE debt).
ORDER_TEST_SERIES = {"NSE": {"EQ", "BE"}, "BSE": {"A", "B", "T", "X", "XT", "M", "MT", "Z"}}
ALLOWED_PRODUCTS = {"C": "CNC (delivery)", "I": "MIS (intraday)"}
MIN_OFFSET_PCT, DEFAULT_OFFSET_PCT, MAX_OFFSET_PCT = 1.0, 3.0, 10.0
MAX_ORDER_OPS_PER_SECOND = 8     # TradeSmart limit is 10 orders/s; PlaceOrder and CancelOrder both count
# The gate counts over 1.25 s rather than 1 s: requests spaced on the client can arrive bunched up at the
# server (network jitter), so the extra 0.25 s keeps the server-side count in any 1 s at or below the cap.
ORDER_RATE_WINDOW_S = 1.25
MAX_BURST_SIZE = DEFAULT_BURST_SIZE = 8
MIN_BURST_PAUSE_S, DEFAULT_BURST_PAUSE_S, MAX_BURST_PAUSE_S = 1.0, 3.0, 60.0
STATUS_POLLS = 3
STATUS_POLL_INTERVAL_S = 0.5
SAFE_FINAL_STATUSES = {"CANCELED", "CANCELLED", "REJECTED"}
OPEN_ORDER_STATUSES = {"OPEN", "PENDING", "TRIGGER_PENDING"}
TEST_ORDER_REMARK = "latency-test"  # tags every test order; recovery only ever cancels orders with this tag
SHUTDOWN_WAIT_S = 25.0
# Set when a placement got no reply: an order may exist that the journal can't list (it has no number yet).
NEEDS_CHECK_SETTING = "order_test_needs_check"
IST = timezone(timedelta(hours=5, minutes=30))
REQUIRED_ENDPOINTS = ("GetQuotes", "PlaceOrder", "CancelOrder", "SingleOrdHist")
RECOVERY_ENDPOINTS = ("OrderBook", "CancelOrder", "SingleOrdHist")


class OrderTestError(RuntimeError):
    """Raised when a guardrail refuses the request. Nothing has been sent when this is raised."""


def confirmation_phrase(count: int) -> str:
    """The exact text the user must type to place ``count`` orders."""
    return f"PLACE {int(count)} ORDER{'S' if int(count) != 1 else ''}"


# ----------------------------------------------------------------------------------------- quote & price
@dataclass
class Quote:
    tsym: str
    ltp: Decimal
    tick: Decimal
    lower_circuit: Decimal | None
    upper_circuit: Decimal | None


def _dec(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d > 0 else None


def parse_quote(body: bytes | None) -> Quote:
    """Parse a GetQuotes body into a :class:`Quote` (raises :class:`OrderTestError` when unusable)."""
    try:
        data = json.loads(body or b"")
    except ValueError as exc:
        raise OrderTestError("the quote response is not valid JSON") from exc
    if not isinstance(data, dict) or str(data.get("stat", "")).lower() != "ok":
        emsg = data.get("emsg") if isinstance(data, dict) else None
        raise OrderTestError(f"no quote for this instrument ({emsg or 'unexpected response'}). "
                             "Check the exchange and token.")
    ltp, tick = _dec(data.get("lp")), _dec(data.get("ti")) or Decimal("0.05")
    if ltp is None:
        raise OrderTestError("the quote has no last traded price (lp); the instrument may not be trading")
    return Quote(tsym=str(data.get("tsym") or ""), ltp=ltp, tick=tick,
                 lower_circuit=_dec(data.get("lc")), upper_circuit=_dec(data.get("uc")))


def safe_limit_price(q: Quote, offset_pct: float) -> Decimal:
    """BUY limit price ``offset_pct`` below the live price, on the tick grid, inside the circuit band.

    Raises :class:`OrderTestError` when the result would sit less than ``MIN_OFFSET_PCT`` below the live
    price (e.g. the stock is near its lower circuit), because such an order could fill.
    """
    offset = Decimal(str(min(max(offset_pct, MIN_OFFSET_PCT), MAX_OFFSET_PCT)))
    raw = q.ltp * (Decimal(1) - offset / Decimal(100))
    price = (raw / q.tick).to_integral_value(rounding=ROUND_FLOOR) * q.tick
    if q.lower_circuit is not None and price < q.lower_circuit:
        price = (q.lower_circuit / q.tick).to_integral_value(rounding=ROUND_CEILING) * q.tick
    ceiling = q.ltp * (Decimal(1) - Decimal(str(MIN_OFFSET_PCT)) / Decimal(100))
    if price <= 0 or price > ceiling:
        raise OrderTestError(f"can't place a safe order: the lower circuit is within {MIN_OFFSET_PCT:g}% of the "
                             "live price. Pick a different instrument.")
    return price.quantize(q.tick) if q.tick.as_tuple().exponent < 0 else price


# ----------------------------------------------------------------------------------------- spec & payloads
@dataclass
class OrderSpec:
    exch: str
    tsym: str
    token: str
    product: str = "C"
    offset_pct: float = DEFAULT_OFFSET_PCT

    def problems(self) -> list[str]:
        out = []
        if self.exch not in ALLOWED_EXCHANGES:
            out.append(f"exchange must be one of {', '.join(ALLOWED_EXCHANGES)} (cash equities only)")
        if not self.tsym.strip():
            out.append("enter the trading symbol, e.g. IDEA-EQ")
        if not self.token.strip().isdigit():
            out.append("enter the numeric instrument token (from the Script Master or SearchScrip)")
        if self.product not in ALLOWED_PRODUCTS:
            out.append(f"product must be one of {', '.join(ALLOWED_PRODUCTS)}")
        if not (MIN_OFFSET_PCT <= float(self.offset_pct) <= MAX_OFFSET_PCT):
            out.append(f"price offset must be between {MIN_OFFSET_PCT:g}% and {MAX_OFFSET_PCT:g}%")
        return out


def _same_symbol(a: str, b: str) -> bool:
    return unquote(a).strip().upper() == unquote(b).strip().upper()


def build_order_payload(client_id: str, spec: OrderSpec, price: Decimal) -> dict:
    """PlaceOrder body: BUY 1 @ LIMIT, DAY. ``tsym`` special characters URL-encoded as the docs require."""
    return {
        "uid": client_id, "actid": client_id, "exch": spec.exch,
        "tsym": quote(unquote(spec.tsym.strip()), safe="-_."),
        "qty": str(ORDER_QTY), "prc": str(price), "dscqty": "0", "prd": spec.product,
        "trantype": "B", "prctyp": "LMT", "ret": "DAY", "ordersource": "API", "remarks": TEST_ORDER_REMARK,
    }


# ----------------------------------------------------------------------------------------- daily cap
def ist_today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(IST).date().isoformat()


def orders_placed_today(db: "Database", now: datetime | None = None) -> int:
    """Number of PlaceOrder attempts (any outcome) stored for the current IST day."""
    df = db.get_results(endpoint_ids=["PlaceOrder"], include_warmup=True, include_synthetic=False)
    if df is None or df.empty:
        return 0
    stamps = pd.to_datetime(df["started_at"], utc=True, errors="coerce")
    return int((stamps.dt.tz_convert(IST).dt.date.astype(str) == ist_today(now)).sum())


def remaining_today(db: "Database", now: datetime | None = None) -> int:
    return max(0, MAX_ORDERS_PER_DAY - orders_placed_today(db, now))


# ----------------------------------------------------------------------------------------- run
class OrderRateGate:
    """Sliding window over order operations (PlaceOrder + CancelOrder).

    TradeSmart allows 10 orders per second; this gate keeps at most ``max_per_second`` (hard-capped at
    :data:`MAX_ORDER_OPS_PER_SECOND`) in any rolling :data:`ORDER_RATE_WINDOW_S` window, counting
    cancellations as order operations. The window is longer than one second to absorb network jitter.
    """

    def __init__(self, max_per_second: int = MAX_ORDER_OPS_PER_SECOND,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.max_per_second = max(1, min(int(max_per_second), MAX_ORDER_OPS_PER_SECOND))
        self._clock, self._sleep = clock, sleep
        self._sent: deque[float] = deque()

    def acquire(self) -> float:
        """Block until one more order operation fits in the window; returns seconds waited."""
        waited = 0.0
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0] >= ORDER_RATE_WINDOW_S:
                self._sent.popleft()
            if len(self._sent) < self.max_per_second:
                self._sent.append(now)
                return waited
            pause = ORDER_RATE_WINDOW_S - (now - self._sent[0]) + 0.001
            self._sleep(pause)
            waited += pause


@dataclass
class OrderAttempt:
    seq: int
    ltp: Decimal | None = None
    price: Decimal | None = None
    place: "RequestResult | None" = None
    norenordno: str | None = None
    cancel: "RequestResult | None" = None
    final_status: str | None = None
    filled: bool = False
    alert: str | None = None


@dataclass
class OrderTestOutcome:
    run_id: str
    attempts: list[OrderAttempt] = field(default_factory=list)
    stop_reason: str | None = None
    stopped_by_user: bool = False
    started_by: str = "local"

    @property
    def alerts(self) -> list[str]:
        return [a.alert for a in self.attempts if a.alert]


def _json(body: bytes | None) -> Any:
    try:
        return json.loads(body or b"")
    except ValueError:
        return None


def _order_status(body: bytes | None) -> tuple[str | None, bool]:
    """(latest status, filled?) from a SingleOrdHist body. Filled = any entry COMPLETE or fillshares > 0."""
    data = _json(body)
    if not isinstance(data, list) or not data:
        return None, False
    filled = False
    for entry in data:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status", "")).upper()
        try:
            fills = float(entry.get("fillshares") or 0)
        except (TypeError, ValueError):
            fills = 0.0
        filled = filled or status == "COMPLETE" or fills > 0
    first = data[0] if isinstance(data[0], dict) else {}
    return (str(first.get("status") or "").upper() or None), filled


_active_batches: list[tuple[threading.Event, threading.Thread]] = []


@atexit.register
def _stop_batches_on_shutdown() -> None:
    """On service stop: place no more orders and give the running burst time to cancel what it placed."""
    for stop, _ in list(_active_batches):
        stop.set()
    deadline = time.monotonic() + SHUTDOWN_WAIT_S
    for _, thread in list(_active_batches):
        if thread is not threading.current_thread():
            thread.join(max(0.0, deadline - time.monotonic()))


def run_order_test(
    db: "Database",
    endpoints: dict[str, "Endpoint"],
    client: "TradeSmartClient",
    *,
    started_by: str = "local",
    ip_guard: Callable[[], str | None] | None = None,
    stop_event: threading.Event | None = None,
    **kwargs: Any,
) -> OrderTestOutcome:
    """Run one guarded order batch: server-wide lock + static-IP guard around :func:`_run_order_batch`.

    ``ip_guard`` returns why orders must be refused, or None. :class:`OrderTestError` means nothing was sent.
    """
    stop_event = stop_event or threading.Event()
    try:
        with run_lock.exclusive_run(run_lock.lock_path_for(db.path), owner=started_by, kind="order test"):
            reason = ip_guard() if ip_guard else None
            if reason:
                raise OrderTestError(reason)
            entry = (stop_event, threading.current_thread())
            _active_batches.append(entry)
            try:
                return _run_order_batch(db, endpoints, client, started_by=started_by, stop_event=stop_event, **kwargs)
            finally:
                _active_batches.remove(entry)
    except run_lock.RunBusyError as exc:
        raise OrderTestError(str(exc)) from exc


def _run_order_batch(
    db: "Database",
    endpoints: dict[str, "Endpoint"],
    client: "TradeSmartClient",
    *,
    started_by: str,
    client_id: str,
    spec: OrderSpec,
    count: int,
    confirmation: str,
    run_id: str,
    environment: str,
    base_url: str,
    timeout_s: float,
    burst_size: int = DEFAULT_BURST_SIZE,
    burst_pause_s: float = DEFAULT_BURST_PAUSE_S,
    max_ops_per_second: int = MAX_ORDER_OPS_PER_SECOND,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: datetime | None = None,
    stop_event: threading.Event | None = None,
    on_attempt: Callable[[OrderAttempt], None] | None = None,
) -> OrderTestOutcome:
    """Place ``count`` guarded BUY LIMIT orders automatically in bursts, cancelling every one.

    Each burst: one fresh quote -> place up to ``burst_size`` orders back to back -> cancel all of them ->
    confirm their final status. The next burst's first order is sent no sooner than ``burst_pause_s``
    after the previous burst's last order. PlaceOrder and CancelOrder both pass :class:`OrderRateGate`.

    Every guardrail is checked first; :class:`OrderTestError` means nothing was sent. Request errors after
    that never raise: every accepted order of the current burst is still cancelled, then the batch stops
    and the problem is reported on the attempts. ``stop_event`` stops the batch cleanly (orders already
    placed in the current burst are still cancelled).
    """
    missing_eps = [e for e in REQUIRED_ENDPOINTS if e not in endpoints]
    if missing_eps:
        raise OrderTestError(f"endpoint registry is missing: {', '.join(missing_eps)}")
    problems = spec.problems()
    if not client_id:
        problems.append("TS_CLIENT_ID is not configured")
    remaining = remaining_today(db, now)
    if not 1 <= int(count) <= remaining:
        problems.append(f"order count must be between 1 and {remaining} (daily cap {MAX_ORDERS_PER_DAY}, "
                        f"{MAX_ORDERS_PER_DAY - remaining} used today)")
    if not 1 <= int(burst_size) <= MAX_BURST_SIZE:
        problems.append(f"orders per burst must be between 1 and {MAX_BURST_SIZE}")
    if not MIN_BURST_PAUSE_S <= float(burst_pause_s) <= MAX_BURST_PAUSE_S:
        problems.append(f"delay between bursts must be between {MIN_BURST_PAUSE_S:g} and {MAX_BURST_PAUSE_S:g} s")
    if confirmation.strip() != confirmation_phrase(count):
        problems.append(f'type "{confirmation_phrase(count)}" exactly to confirm')
    unresolved = db.unresolved_test_orders()
    if unresolved:
        problems.append(f"{len(unresolved)} test order(s) from an earlier batch are not confirmed closed; "
                        "run the open-order check first")
    if db.get_setting(NEEDS_CHECK_SETTING):
        problems.append("an earlier order got no reply and may exist; run the open-order check first")
    if problems:
        raise OrderTestError("; ".join(problems))

    ep_q, ep_place, ep_cancel, ep_hist = (endpoints[e] for e in REQUIRED_ENDPOINTS)
    count, burst_size, burst_pause_s = int(count), int(burst_size), float(burst_pause_s)
    gate = OrderRateGate(max_ops_per_second, clock=clock, sleep=sleep)
    config = {"endpoint_ids": ["PlaceOrder", "CancelOrder"], "iterations": count, "warmup": 0,
              "timeout_s": timeout_s, "delay_ms": int(burst_pause_s * 1000),
              "connection_mode": client.connection_mode, "order": "grouped", "concurrency": 1,
              "environment": environment, "source": "order_test", "exch": spec.exch, "product": spec.product,
              "offset_pct": float(spec.offset_pct), "burst_size": burst_size, "burst_pause_s": burst_pause_s,
              "max_order_ops_per_second": gate.max_per_second, "started_by": started_by}
    db.create_run(run_id=run_id, run_type="http", environment=environment,
                  base_host=urlparse(base_url).hostname or "", connection_mode=client.connection_mode,
                  config=config, planned_requests=2 * count,
                  notes=f"LIVE ORDER TEST: {count} x BUY {ORDER_QTY} {spec.exch}:{spec.tsym} LIMIT, "
                        f"bursts of {burst_size} every {burst_pause_s:g}s, auto-cancelled")
    outcome = OrderTestOutcome(run_id=run_id, started_by=started_by)
    stored: list = []
    unsaved: list = []   # results sent but not yet written; always flushed (see finally)
    journaled: set[str] = set()   # order numbers already in the recovery journal
    status = "completed"

    def flush() -> None:
        if unsaved:
            db.insert_results(list(unsaved))
            unsaved.clear()

    def stop_requested() -> bool:
        return stop_event is not None and stop_event.is_set()

    def notify(attempt: OrderAttempt) -> None:
        if on_attempt:
            try:
                on_attempt(attempt)
            except Exception:  # a display problem must never stop live orders from being cancelled
                logger.exception("Order test %s: progress callback failed", run_id)

    def journal(attempts: list[OrderAttempt]) -> None:
        db.record_test_orders([{"norenordno": a.norenordno, "run_id": run_id, "exch": spec.exch, "tsym": spec.tsym,
                                "started_by": started_by} for a in attempts])
        journaled.update(a.norenordno for a in attempts)

    def check_status(attempt: OrderAttempt) -> bool:
        """One SingleOrdHist read; True when the order is confirmed closed (or found filled)."""
        h = client.timed_post(ep_hist, {"uid": client_id, "norenordno": attempt.norenordno},
                              run_id=run_id, seq=attempt.seq, is_warmup=True)
        attempt.final_status, filled = _order_status(client.last_response_body if h.success else None)
        attempt.filled = attempt.filled or filled
        if filled:
            attempt.alert = (f"Order {attempt.norenordno} was FILLED (status {attempt.final_status}). You now "
                             f"hold {ORDER_QTY} share of {spec.tsym}. Review it in TradeSmart.")
        return filled or attempt.final_status in SAFE_FINAL_STATUSES

    def settle_pending(pending: list[OrderAttempt]) -> None:
        """Re-poll orders not yet confirmed closed; flag any that never close."""
        for _ in range(STATUS_POLLS):
            if not pending:
                return
            sleep(STATUS_POLL_INTERVAL_S)
            pending[:] = [a for a in pending if not check_status(a)]
        for a in pending:
            a.alert = a.alert or (f"Order {a.norenordno} status is {a.final_status or 'unknown'} after "
                                  "cancelling. Check your order book in TradeSmart and cancel it if still open.")

    try:
        seq = 0
        next_burst_at: float | None = None
        while seq < count and not stop_requested():
            if next_burst_at is not None:
                wait = next_burst_at - clock()
                if wait > 0:
                    sleep(wait)
                if stop_requested():
                    break
            burst_end = min(count, seq + burst_size)

            # 1. one fresh quote per burst -> safe price (read-only; not part of the latency samples)
            q_res = client.timed_post(ep_q, {"uid": client_id, "exch": spec.exch, "token": spec.token.strip()},
                                      run_id=run_id, seq=seq + 1, is_warmup=True)
            try:
                if not q_res.success:
                    raise OrderTestError(f"quote request failed ({q_res.error_message or q_res.outcome})")
                q = parse_quote(client.last_response_body)
                if q.tsym and not _same_symbol(q.tsym, spec.tsym):
                    raise OrderTestError(f"token {spec.token} is {q.tsym}, not {spec.tsym}. Fix the symbol or token.")
                price = safe_limit_price(q, spec.offset_pct)
                if price * ORDER_QTY > MAX_ORDER_VALUE_INR:
                    raise OrderTestError(f"order value ₹{price * ORDER_QTY} is above the "
                                         f"₹{MAX_ORDER_VALUE_INR} cap. Pick a lower-priced instrument.")
            except OrderTestError as exc:
                attempt = OrderAttempt(seq=seq + 1, alert=f"Order {seq + 1} not placed: {exc}")
                outcome.attempts.append(attempt)
                outcome.stop_reason = attempt.alert
                break

            # 2. place the whole burst back to back (<= max_ops_per_second order requests per window)
            burst: list[OrderAttempt] = []
            while seq < burst_end and not stop_requested():
                seq += 1
                attempt = OrderAttempt(seq=seq, ltp=q.ltp, price=price)
                outcome.attempts.append(attempt)
                gate.acquire()
                attempt.place = client.timed_post(ep_place, build_order_payload(client_id, spec, price),
                                                  run_id=run_id, seq=seq, is_warmup=False)
                stored.append(attempt.place)
                unsaved.append(attempt.place)
                body = _json(client.last_response_body)
                if isinstance(body, dict) and body.get("norenordno"):
                    attempt.norenordno = str(body["norenordno"])
                burst.append(attempt)
                notify(attempt)
                if not attempt.norenordno:
                    why = attempt.place.error_message or attempt.place.outcome
                    if attempt.place.outcome in ("timeout", "connect_error", "other_error"):
                        attempt.alert = (f"Order {seq}: no reply from TradeSmart ({why}). The order MAY have been "
                                         "placed. Run the open-order check, and confirm in your TradeSmart order book.")
                        db.set_setting(NEEDS_CHECK_SETTING, True)
                    else:
                        attempt.alert = f"Order {seq} was not accepted: {why}"
                    break  # stop placing; the accepted orders of this burst are still cancelled below
            next_burst_at = clock() + burst_pause_s
            # Persist the burst's placements right away (the daily cap counts stored attempts), and journal the
            # accepted orders so a crash from here on can be recovered. Written after the placement loop, not
            # between orders, so a database lock can't stretch the burst.
            accepted = [a for a in burst if a.norenordno]
            save_problem = None
            try:
                flush()
                journal(accepted)
            except Exception as exc:  # the orders are live, so cancel them anyway; recovery can find them by tag
                logger.exception("Order test %s: couldn't save the burst", run_id)
                save_problem = f"Couldn't save this burst to the database ({exc}). Batch stopped after cancelling it."

            # 3. cancel EVERY accepted order of the burst, even if one cancel fails
            for attempt in accepted:
                gate.acquire()
                attempt.cancel = client.timed_post(ep_cancel, {"uid": client_id, "norenordno": attempt.norenordno},
                                                   run_id=run_id, seq=attempt.seq, is_warmup=False)
                stored.append(attempt.cancel)
                unsaved.append(attempt.cancel)
                if not attempt.cancel.success:
                    attempt.alert = (f"Cancel FAILED for order {attempt.norenordno} "
                                     f"({attempt.cancel.error_message or attempt.cancel.outcome}). Cancel it in "
                                     "TradeSmart now.")

            flush()

            # 4. confirm the final state of every accepted order (read-only)
            pending = [a for a in accepted if not check_status(a)]
            settle_pending(pending)
            for attempt in accepted:
                state = ("filled" if attempt.filled else
                         "closed" if attempt.final_status in SAFE_FINAL_STATUSES else "open")
                db.update_test_order(attempt.norenordno, state, attempt.final_status)
                notify(attempt)

            problem = next((a for a in burst if a.alert), None)
            if problem is not None:
                outcome.stop_reason = problem.alert
                break
            if save_problem:
                outcome.stop_reason = save_problem
                break
        if stop_requested() and outcome.stop_reason is None and seq < count:
            outcome.stopped_by_user = True
            outcome.stop_reason = f"Stopped by you after {seq} of {count} order(s)."
    except Exception as exc:  # never leave the run half-recorded
        status = "failed"
        outcome.stop_reason = f"Order test stopped unexpectedly: {exc}. Check your order book in TradeSmart."
        logger.exception("Order test %s failed", run_id)
    finally:
        try:  # a failure inside a burst can skip the journal step; record_test_orders keeps existing states
            journal([a for a in outcome.attempts if a.norenordno and a.norenordno not in journaled])
            flush()
        except Exception as exc:
            status = "failed"
            outcome.stop_reason = outcome.stop_reason or f"Couldn't save the results to the database: {exc}"
            logger.exception("Order test %s: couldn't save the results", run_id)
        if stored:
            df = pd.DataFrame([r.to_row() for r in stored])
            db.save_endpoint_metrics(run_id, endpoint_metrics(df))
        if outcome.stop_reason and status == "completed":
            status = "stopped"
        db.finish_run(run_id, status, outcome.stop_reason)
        logger.info("Order test %s: %d order attempt(s), status %s", run_id, len(outcome.attempts), status)
    return outcome


# ----------------------------------------------------------------------------------------- recovery
@dataclass
class ReconcileReport:
    """Result of :func:`reconcile_test_orders`. Each list holds order numbers."""

    checked: int = 0
    closed: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    filled: list[str] = field(default_factory=list)
    still_open: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        return bool(self.filled or self.still_open or self.errors)


def reconcile_test_orders(
    db: "Database",
    endpoints: dict[str, "Endpoint"],
    client: "TradeSmartClient",
    *,
    client_id: str,
    started_by: str = "recovery",
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ReconcileReport:
    """Find test orders left open (e.g. by a crash mid-burst) and cancel them.

    Looks at the local journal plus today's order book, but only ever touches orders from the journal or tagged
    ``TEST_ORDER_REMARK``, so real orders are never cancelled. An order whose status can't be read is not cancelled
    and stays unresolved. Raises :class:`OrderTestError` when another run holds the lock.
    """
    missing_eps = [e for e in RECOVERY_ENDPOINTS if e not in endpoints]
    if missing_eps:
        raise OrderTestError(f"endpoint registry is missing: {', '.join(missing_eps)}")
    report = ReconcileReport()
    run_id = "recovery"
    gate = OrderRateGate(MAX_ORDER_OPS_PER_SECOND, clock=clock, sleep=sleep)

    def status_of(number: str) -> tuple[str | None, bool]:
        h = client.timed_post(endpoints["SingleOrdHist"], {"uid": client_id, "norenordno": number},
                              run_id=run_id, seq=0, is_warmup=True)
        return _order_status(client.last_response_body if h.success else None)

    try:
        with run_lock.exclusive_run(run_lock.lock_path_for(db.path), owner=started_by, kind="open-order check"):
            targets = {row["norenordno"] for row in db.unresolved_test_orders()}

            book = client.timed_post(endpoints["OrderBook"], {"uid": client_id, "actid": client_id},
                                     run_id=run_id, seq=0, is_warmup=True)
            rows = _json(client.last_response_body) if book.success else None  # "no data" counts as success
            if not book.success:
                report.errors.append(f"Couldn't read the order book: {book.error_message or book.outcome}")
            for row in rows if isinstance(rows, list) else []:
                if (isinstance(row, dict) and row.get("norenordno") and row.get("remarks") == TEST_ORDER_REMARK
                        and str(row.get("status", "")).upper() in OPEN_ORDER_STATUSES):
                    number = str(row["norenordno"])
                    if number not in targets:
                        db.record_test_orders([{"norenordno": number, "run_id": run_id, "exch": row.get("exch"),
                                                "tsym": row.get("tsym"), "started_by": started_by}])
                        targets.add(number)

            if book.success:  # the order book was read, so a no-reply order would now be in targets
                db.set_setting(NEEDS_CHECK_SETTING, False)

            for number in sorted(targets):
                report.checked += 1
                final, filled = status_of(number)
                cancel = None
                if not filled and final is not None and final not in SAFE_FINAL_STATUSES:
                    gate.acquire()
                    cancel = client.timed_post(endpoints["CancelOrder"], {"uid": client_id, "norenordno": number},
                                               run_id=run_id, seq=0, is_warmup=False)
                    for _ in range(STATUS_POLLS):
                        sleep(STATUS_POLL_INTERVAL_S)
                        final, filled = status_of(number)
                        if filled or final in SAFE_FINAL_STATUSES:
                            break
                if filled:
                    db.update_test_order(number, "filled", final)
                    report.filled.append(number)
                elif final in SAFE_FINAL_STATUSES:
                    db.update_test_order(number, "closed", final)
                    (report.cancelled if cancel is not None and cancel.success else report.closed).append(number)
                else:
                    db.update_test_order(number, "open", final)
                    report.still_open.append(number)
                    if cancel is not None and not cancel.success:
                        report.errors.append(f"Cancel failed for {number}: {cancel.error_message or cancel.outcome}")
    except run_lock.RunBusyError as exc:
        raise OrderTestError(str(exc)) from exc
    logger.info("Open-order check by %s: checked %d, cancelled %d, filled %d, still open %d, errors %d",
                started_by, report.checked, len(report.cancelled), len(report.filled), len(report.still_open),
                len(report.errors))
    return report
