"""Tests for the guarded live order-latency test. A fake TradeSmart (httpx.MockTransport) records every call."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import unquote

import httpx
import pytest

from src.api_client import TradeSmartClient
from src.benchmark_engine import BenchmarkConfig, BenchmarkRunner
from src.database import Database
from src.endpoint_registry import Endpoint
from src.order_test import (
    MAX_ORDER_OPS_PER_SECOND, MAX_ORDER_VALUE_INR, ORDER_RATE_WINDOW_S, MAX_ORDERS_PER_DAY, OrderRateGate, OrderSpec, OrderTestError,
    Quote, build_order_payload,
    confirmation_phrase, orders_placed_today, parse_quote, remaining_today, run_order_test, safe_limit_price,
)

BASE = "https://mock.tradesmart.test/NorenWClientAPIv2"
NOW = datetime.now(timezone.utc)  # stored results use the real clock, so the daily cap must too
EPS = {
    "GetQuotes": Endpoint(id="GetQuotes", name="Get Quotes", category="Scrips", path="/GetQuotes"),
    "PlaceOrder": Endpoint(id="PlaceOrder", name="Place Order", category="Orders", path="/PlaceOrder",
                           safety_class="state_changing", benchmark_enabled=False),
    "CancelOrder": Endpoint(id="CancelOrder", name="Cancel Order", category="Orders", path="/CancelOrder",
                            safety_class="state_changing", benchmark_enabled=False),
    "SingleOrdHist": Endpoint(id="SingleOrdHist", name="Order History", category="Orders", path="/SingleOrdHist"),
}
SPEC = OrderSpec(exch="NSE", tsym="IDEA-EQ", token="14366", product="C", offset_pct=3.0)


class FakeTradeSmart:
    """Routes by path. Override attributes to script failures."""

    def __init__(self, **kw):
        self.calls: list[tuple[str, dict]] = []
        self.quote = {"stat": "Ok", "tsym": "IDEA-EQ", "lp": "10.00", "ti": "0.01", "lc": "9.00", "uc": "11.00"}
        self.place = lambda n: {"stat": "Ok", "norenordno": f"2510190000{n:04d}"}
        self.cancel = {"stat": "Ok", "result": "x"}
        self.history = [{"status": "CANCELED", "fillshares": "0"}]
        self.t = None                      # FakeTime, set by run(); stamps calls with the fake clock
        self.stamps: list[tuple[str, float]] = []
        self.__dict__.update(kw)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content.decode().removeprefix("jData="))
        self.calls.append((name, payload))
        self.stamps.append((name, self.t.now if self.t else 0.0))
        if name == "GetQuotes":
            return httpx.Response(200, json=self.quote)
        if name == "PlaceOrder":
            out = self.place(sum(1 for c in self.calls if c[0] == "PlaceOrder"))
            return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)
        if name == "CancelOrder":
            return httpx.Response(200, json=self.cancel)
        if name == "SingleOrdHist":
            return httpx.Response(200, json=self.history)
        raise AssertionError(f"unexpected endpoint {name}")

    def sent(self, name: str) -> list[dict]:
        return [p for n, p in self.calls if n == name]


class FakeTime:
    """Deterministic clock; sleep advances it. ``slept`` records every pause."""

    def __init__(self):
        self.now, self.slept = 1000.0, []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def run(db, fake, count=2, confirmation=None, spec=SPEC, t=None, **kw):
    t = t or FakeTime()
    fake.t = t
    with TradeSmartClient(BASE, access_token="TOK", transport=httpx.MockTransport(fake)) as client:
        return run_order_test(db, EPS, client, client_id="AB1234", spec=spec, count=count,
                              confirmation=confirmation_phrase(count) if confirmation is None else confirmation,
                              run_id=f"ord-{len(db.list_runs())}", environment="live", base_url=BASE,
                              timeout_s=10, sleep=t.sleep, clock=t.clock, now=NOW, **kw)


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


def test_happy_path_places_and_cancels_each_order(db):
    fake = FakeTradeSmart()
    out = run(db, fake, count=2)
    assert out.stop_reason is None and out.alerts == []
    # one quote per burst, then the whole burst is placed, then cancelled, then confirmed
    assert [n for n, _ in fake.calls] == ["GetQuotes"] + ["PlaceOrder"] * 2 + ["CancelOrder"] * 2 \
        + ["SingleOrdHist"] * 2
    for a in out.attempts:
        assert a.place.success and a.cancel.success and a.final_status == "CANCELED"
    run_row = db.get_run(out.run_id)
    assert run_row["status"] == "completed" and run_row["config"]["source"] == "order_test"
    res = db.get_results([out.run_id])
    assert sorted(res["endpoint_id"].tolist()) == ["CancelOrder"] * 2 + ["PlaceOrder"] * 2  # quotes not stored
    assert set(db.get_metrics([out.run_id])["endpoint_id"]) == {"PlaceOrder", "CancelOrder"}


def test_order_payload_is_buy_limit_day_qty1_below_market(db):
    fake = FakeTradeSmart()
    run(db, fake, count=1)
    body = fake.sent("PlaceOrder")[0]
    assert body["trantype"] == "B" and body["prctyp"] == "LMT" and body["ret"] == "DAY" and body["qty"] == "1"
    assert Decimal(body["prc"]) == Decimal("9.70")      # 3 % below 10.00 on a 0.01 tick
    assert body["uid"] == body["actid"] == "AB1234" and body["exch"] == "NSE"
    assert fake.sent("CancelOrder")[0]["norenordno"] == "25101900000001"


@pytest.mark.parametrize("kw,needle", [
    (dict(confirmation="place 2 orders"), "type"),
    (dict(confirmation="PLACE 3 ORDERS"), "type"),
    (dict(count=MAX_ORDERS_PER_DAY + 1), "between 1 and"),
    (dict(spec=OrderSpec(exch="NFO", tsym="X", token="1")), "exchange"),
    (dict(spec=OrderSpec(exch="NSE", tsym="IDEA-EQ", token="abc")), "token"),
    (dict(spec=OrderSpec(exch="NSE", tsym="IDEA-EQ", token="1", product="M")), "product"),
    (dict(spec=OrderSpec(exch="NSE", tsym="IDEA-EQ", token="1", offset_pct=0.5)), "offset"),
])
def test_guardrails_refuse_before_anything_is_sent(db, kw, needle):
    fake = FakeTradeSmart()
    with pytest.raises(OrderTestError, match=needle):
        run(db, fake, **kw)
    assert fake.calls == [] and db.list_runs().empty


def test_daily_cap_counts_stored_attempts_across_runs(db, monkeypatch):
    import src.order_test as ot

    monkeypatch.setattr(ot, "MAX_ORDERS_PER_DAY", 12)   # keep the test fast; the real cap is checked below
    run(db, FakeTradeSmart(), count=4)
    assert orders_placed_today(db, NOW) == 4 and remaining_today(db, NOW) == 8
    run(db, FakeTradeSmart(), count=8)
    fake = FakeTradeSmart()
    with pytest.raises(OrderTestError, match="between 1 and 0"):
        run(db, fake, count=1)
    assert fake.calls == []


def test_symbol_token_mismatch_stops_before_placing(db):
    fake = FakeTradeSmart(quote={"stat": "Ok", "tsym": "SBIN-EQ", "lp": "800", "ti": "0.05"})
    out = run(db, fake, count=2)
    assert fake.sent("PlaceOrder") == [] and "SBIN-EQ" in out.stop_reason


def test_value_cap(db):
    fake = FakeTradeSmart(quote={"stat": "Ok", "tsym": "IDEA-EQ", "lp": "9000", "ti": "0.05"})
    out = run(db, fake, count=1)
    assert fake.sent("PlaceOrder") == [] and str(MAX_ORDER_VALUE_INR) in out.stop_reason


def test_rejected_placement_stops_batch_without_cancel(db):
    fake = FakeTradeSmart(place=lambda n: {"stat": "Not_Ok", "emsg": "Insufficient balance"})
    out = run(db, fake, count=3)
    assert len(fake.sent("PlaceOrder")) == 1 and fake.sent("CancelOrder") == []
    assert "Insufficient balance" in out.stop_reason and db.get_run(out.run_id)["status"] == "stopped"


def test_rejection_mid_burst_still_cancels_the_orders_already_accepted(db):
    fake = FakeTradeSmart(place=lambda n: {"stat": "Ok", "norenordno": f"2510190000{n:04d}"} if n < 3
                          else {"stat": "Not_Ok", "emsg": "RMS: margin exceeded"})
    out = run(db, fake, count=8)
    assert len(fake.sent("PlaceOrder")) == 3                    # stops placing at the rejection
    assert [c["norenordno"] for c in fake.sent("CancelOrder")] == ["25101900000001", "25101900000002"]
    assert "margin exceeded" in out.stop_reason


def test_timeout_on_placement_warns_order_may_exist(db):
    def boom(n):
        raise httpx.ReadTimeout("timed out")
    out = run(db, FakeTradeSmart(place=boom), count=2)
    assert "MAY have been placed" in out.stop_reason


def test_failed_cancel_raises_alert_and_stops(db):
    fake = FakeTradeSmart(cancel={"stat": "Not_Ok", "emsg": "Order already processed"},
                          history=[{"status": "OPEN"}])
    out = run(db, fake, count=12)
    assert len(fake.sent("PlaceOrder")) == 8                    # first burst only, then the batch stops
    assert len(fake.sent("CancelOrder")) == 8                   # every order still gets a cancel attempt
    assert "Cancel FAILED" in out.alerts[0] and "25101900000001" in out.alerts[0]


def test_fill_detected_raises_alert_and_stops(db):
    fake = FakeTradeSmart(history=[{"status": "COMPLETE", "fillshares": "1"}])
    out = run(db, fake, count=12)
    assert len(fake.sent("PlaceOrder")) == 8 and len(fake.sent("CancelOrder")) == 8
    assert "FILLED" in out.alerts[0] and "FILLED" in out.stop_reason


def test_status_still_open_after_cancel_alerts(db):
    fake = FakeTradeSmart(history=[{"status": "OPEN"}])
    out = run(db, fake, count=1)
    assert len(fake.sent("SingleOrdHist")) == 4 and "still open" in out.alerts[0]  # 1 immediate + 3 re-polls


@pytest.mark.parametrize("q,offset,expected", [
    (Quote("X", Decimal("100"), Decimal("0.05"), Decimal("80"), None), 3.0, Decimal("97.00")),
    (Quote("X", Decimal("101.37"), Decimal("0.05"), None, None), 3.0, Decimal("98.30")),   # floor to tick
    (Quote("X", Decimal("100"), Decimal("0.05"), Decimal("98.02"), None), 5.0, Decimal("98.05")),  # clamp to lc
])
def test_safe_limit_price(q, offset, expected):
    assert safe_limit_price(q, offset) == expected


def test_safe_limit_price_refuses_near_lower_circuit():
    with pytest.raises(OrderTestError, match="lower circuit"):
        safe_limit_price(Quote("X", Decimal("100"), Decimal("0.05"), Decimal("99.5"), None), 3.0)


def test_parse_quote_errors():
    with pytest.raises(OrderTestError):
        parse_quote(b'{"stat":"Not_Ok","emsg":"no data"}')
    with pytest.raises(OrderTestError):
        parse_quote(b'{"stat":"Ok","tsym":"X"}')


def test_special_characters_in_symbol_are_url_encoded():
    body = build_order_payload("AB1", OrderSpec(exch="NSE", tsym="M&M-EQ", token="1"), Decimal("10"))
    assert body["tsym"] == "M%26M-EQ" and unquote(body["tsym"]) == "M&M-EQ"


def test_generic_benchmark_runner_still_never_sends_place_order(db):
    def handler(request):
        if "PlaceOrder" in request.url.path or "CancelOrder" in request.url.path:
            pytest.fail("the benchmark runner must never send order endpoints")
        return httpx.Response(200, json={"stat": "Ok"})

    runner = BenchmarkRunner(db, list(EPS.values()), lambda c: TradeSmartClient(
        BASE, "TOK", transport=httpx.MockTransport(handler)), BASE, sleep=lambda s: None)
    rid = runner.run(BenchmarkConfig(endpoint_ids=["PlaceOrder", "CancelOrder"], iterations=2, warmup=0,
                                     delay_ms=0, environment="live", payload_context={"ClientId": "AB1"}))
    assert db.get_results([rid]).empty


def _bursts(fake) -> list[list[float]]:
    """PlaceOrder fake-clock times grouped by burst (a GetQuotes call starts each burst)."""
    groups: list[list[float]] = []
    for name, ts in fake.stamps:
        if name == "GetQuotes":
            groups.append([])
        elif name == "PlaceOrder":
            groups[-1].append(ts)
    return groups


def test_one_input_runs_all_bursts_automatically_with_delay(db):
    fake, t = FakeTradeSmart(), FakeTime()
    out = run(db, fake, count=20, t=t, burst_size=8, burst_pause_s=3.0)
    names = [n for n, _ in fake.calls]
    assert names.count("PlaceOrder") == 20 and names.count("CancelOrder") == 20
    groups = _bursts(fake)
    assert [len(g) for g in groups] == [8, 8, 4]                 # one fresh quote per burst
    for g in groups:                                             # 8 placements back to back (< 1 s)
        assert max(g) - min(g) < 1.0
    for prev, nxt in zip(groups, groups[1:]):                    # >= 3 s from a burst's last order to the next
        assert min(nxt) - max(prev) >= 3.0 - 1e-9
    assert out.stop_reason is None and len(out.attempts) == 20


def test_order_requests_never_exceed_limit_in_any_window(db):
    fake, t = FakeTradeSmart(), FakeTime()
    run(db, fake, count=40, t=t, burst_size=8, burst_pause_s=1.0)
    ops = [ts for name, ts in fake.stamps if name in ("PlaceOrder", "CancelOrder")]
    assert len(ops) == 80
    for ts in ops:
        assert sum(1 for x in ops if ts <= x < ts + ORDER_RATE_WINDOW_S) <= MAX_ORDER_OPS_PER_SECOND


def test_rate_gate_keeps_order_ops_within_limit_per_rolling_second():
    t = FakeTime()
    gate = OrderRateGate(50, clock=t.clock, sleep=t.sleep)     # requested 50 -> hard-capped
    assert gate.max_per_second == MAX_ORDER_OPS_PER_SECOND <= 8
    stamps = []
    for _ in range(40):
        gate.acquire()
        stamps.append(t.now)
    for ts in stamps:   # never more than 8 in any 1.25 s window (so jitter can't push a 1 s window past 8)
        assert sum(1 for x in stamps if ts <= x < ts + ORDER_RATE_WINDOW_S) <= MAX_ORDER_OPS_PER_SECOND


def test_place_and_cancel_both_pass_through_the_gate(db, monkeypatch):
    counted = []
    real = OrderRateGate.acquire
    monkeypatch.setattr(OrderRateGate, "acquire", lambda self: counted.append(1) or real(self))
    run(db, FakeTradeSmart(), count=3)
    assert len(counted) == 6                                   # 3 placements + 3 cancels


def test_stop_event_stops_between_orders(db):
    stop = threading.Event()
    fake = FakeTradeSmart()
    out = run(db, fake, count=10, stop_event=stop,
              on_attempt=lambda a: stop.set() if a.seq == 3 else None)
    assert len(fake.sent("PlaceOrder")) == 3 and out.stopped_by_user
    assert len(fake.sent("CancelOrder")) == 3                   # orders already placed are still cancelled
    assert "Stopped by you after 3 of 10" in out.stop_reason
    assert db.get_run(out.run_id)["status"] == "stopped"


def test_daily_cap_is_1000_and_burst_inputs_validated(db):
    assert MAX_ORDERS_PER_DAY == 1000
    fake = FakeTradeSmart()
    with pytest.raises(OrderTestError, match="per burst"):
        run(db, fake, count=2, burst_size=9)
    with pytest.raises(OrderTestError, match="delay between bursts"):
        run(db, fake, count=2, burst_pause_s=0.5)
    with pytest.raises(OrderTestError, match="delay between bursts"):
        run(db, fake, count=2, burst_pause_s=61)
    assert fake.calls == []


def test_unconfirmed_order_is_repolled_after_burst(db):
    replies = iter([[{"status": "PENDING"}], [{"status": "CANCELED"}]])
    fake = FakeTradeSmart()
    fake.history = None
    orig = fake.__call__

    def handler(request):
        if request.url.path.endswith("/SingleOrdHist"):
            fake.calls.append(("SingleOrdHist", {}))
            return httpx.Response(200, json=next(replies))
        return orig(request)

    t = FakeTime()
    with TradeSmartClient(BASE, access_token="TOK", transport=httpx.MockTransport(handler)) as client:
        out = run_order_test(db, EPS, client, client_id="AB1234", spec=SPEC, count=1,
                             confirmation=confirmation_phrase(1), run_id="ord-x", environment="live",
                             base_url=BASE, timeout_s=10, sleep=t.sleep, clock=t.clock, now=NOW)
    assert out.alerts == [] and out.attempts[0].final_status == "CANCELED"
    assert len(fake.sent("SingleOrdHist")) == 2


def test_results_are_saved_even_if_the_run_crashes_mid_burst(db, monkeypatch):
    import src.order_test as ot

    calls = {"n": 0}
    real_status = ot._order_status

    def boom(body):
        calls["n"] += 1
        raise RuntimeError("unexpected crash")

    monkeypatch.setattr(ot, "_order_status", boom)
    out = run(db, FakeTradeSmart(), count=3)
    assert db.get_run(out.run_id)["status"] == "failed" and "unexpectedly" in out.stop_reason
    res = db.get_results([out.run_id])
    assert sorted(res["endpoint_id"].tolist()) == ["CancelOrder"] * 3 + ["PlaceOrder"] * 3
    assert orders_placed_today(db, NOW) == 3                 # daily cap still counts them
