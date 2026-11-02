"""Tests for the order journal, ``reconcile_test_orders`` and the lock/IP guards around ``run_order_test``.

The critical property: recovery only ever cancels orders from the journal or tagged by this app.
"""
from __future__ import annotations

import threading

import httpx
import pytest

import src.order_test as ot
from src import run_lock
from src.api_client import TradeSmartClient
from src.benchmark_engine import BenchmarkConfig, BenchmarkRunner
from src.database import Database
from src.endpoint_registry import Endpoint
from src.order_test import TEST_ORDER_REMARK, OrderTestError, confirmation_phrase, reconcile_test_orders
from tests.test_order_test import BASE, EPS as ORDER_EPS, SPEC, NOW, FakeTime, FakeTradeSmart, run

EPS = {**ORDER_EPS, "OrderBook": Endpoint(id="OrderBook", name="Order Book", category="Orders", path="/OrderBook")}
A, B = "25101900000001", "25101900000002"
CANCELLED = [{"status": "CANCELED", "fillshares": "0"}]
OPEN = [{"status": "OPEN", "fillshares": "0"}]


class Book(FakeTradeSmart):
    """Adds OrderBook and order-by-order history. An order's history flips to cancelled once it is cancelled."""

    def __init__(self, **kw):
        self.book = []
        self.histories = {}          # order number -> history reply (status None -> no history: an error reply)
        self.cancel_works = True
        super().__init__(**kw)

    def __call__(self, request):
        import json

        name = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content.decode().removeprefix("jData="))
        if name == "OrderBook":
            self.calls.append((name, payload))
            return self.book if isinstance(self.book, httpx.Response) else httpx.Response(200, json=self.book)
        if name == "SingleOrdHist" and payload["norenordno"] in self.histories:
            self.calls.append((name, payload))
            reply = self.histories[payload["norenordno"]]
            return httpx.Response(200, json=reply if reply is not None else {"stat": "Not_Ok", "emsg": "broken"})
        if name == "CancelOrder" and self.cancel_works and payload["norenordno"] in self.histories:
            self.histories[payload["norenordno"]] = CANCELLED
        return super().__call__(request)


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


def reconcile(db, fake, **kw):
    t = FakeTime()
    with TradeSmartClient(BASE, access_token="TOK", transport=httpx.MockTransport(fake)) as client:
        return reconcile_test_orders(db, EPS, client, client_id="AB1234", sleep=t.sleep, clock=t.clock, **kw)


def journal(db, *numbers):
    db.record_test_orders([{"norenordno": n, "run_id": "r1", "exch": "NSE", "tsym": "IDEA-EQ", "started_by": "alice"}
                           for n in numbers])


def states(db) -> dict[str, str]:
    with db._connect() as conn:
        return {r["norenordno"]: r["state"] for r in conn.execute("SELECT * FROM order_test_orders")}


def row(number, status="OPEN", remarks=TEST_ORDER_REMARK, **kw):
    return {"norenordno": number, "status": status, "remarks": remarks, "exch": "NSE", "tsym": "IDEA-EQ", **kw}


def cancelled_numbers(fake):
    return [p["norenordno"] for p in fake.sent("CancelOrder")]


# ---------------------------------------------------------------------------- journal
def test_journal_rows_are_created_and_closed_after_a_clean_batch(db):
    run(db, FakeTradeSmart(), count=2, started_by="alice")
    assert states(db) == {A: "closed", B: "closed"} and db.unresolved_test_orders() == []
    with db._connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM order_test_orders ORDER BY norenordno")]
    assert rows[0]["started_by"] == "alice" and rows[0]["final_status"] == "CANCELED"
    assert rows[0]["exch"] == "NSE" and rows[0]["tsym"] == "IDEA-EQ" and rows[0]["run_id"] and rows[0]["placed_at"]


def test_unresolved_rows_are_plain_dicts(db):
    journal(db, A)
    assert db.unresolved_test_orders()[0]["norenordno"] == A


def test_record_is_idempotent_and_keeps_the_existing_state(db):
    journal(db, A)
    db.update_test_order(A, "closed", "CANCELED")
    journal(db, A)
    assert states(db) == {A: "closed"}


def test_journal_is_written_before_any_cancel(db, monkeypatch):
    seen = []
    fake = FakeTradeSmart()
    real = db.record_test_orders
    monkeypatch.setattr(db, "record_test_orders", lambda rows: (seen.append(fake.sent("CancelOrder")[:]), real(rows)))
    run(db, fake, count=1)
    assert seen[0] == []


def test_crash_mid_burst_leaves_orders_open(db, monkeypatch):
    def boom(body):
        raise RuntimeError("unexpected crash")

    monkeypatch.setattr(ot, "_order_status", boom)
    out = run(db, FakeTradeSmart(), count=2)
    assert db.get_run(out.run_id)["status"] == "failed"
    assert states(db) == {A: "open", B: "open"}


def test_failed_burst_in_journal_stays_unresolved_when_order_stays_open(db):
    run(db, FakeTradeSmart(history=OPEN), count=1)
    assert states(db) == {A: "open"}


def test_filled_order_is_journaled_as_filled_and_does_not_block(db):
    run(db, FakeTradeSmart(history=[{"status": "COMPLETE", "fillshares": "1"}]), count=1)
    assert states(db) == {A: "filled"} and db.unresolved_test_orders() == []


def test_new_batch_refused_while_orders_are_unresolved(db):
    journal(db, A)
    fake = FakeTradeSmart()
    with pytest.raises(OrderTestError, match="open-order check"):
        run(db, fake, count=1)
    assert fake.calls == [] and db.list_runs().empty


def test_batch_allowed_again_after_reconcile(db):
    journal(db, A)
    reconcile(db, Book(histories={A: OPEN}))
    assert run(db, FakeTradeSmart(), count=1).stop_reason is None


def test_broken_progress_callback_does_not_stop_the_cancels(db):
    def broken(attempt):
        raise RuntimeError("display error")

    fake = FakeTradeSmart()
    out = run(db, fake, count=2, on_attempt=broken)
    assert len(fake.sent("CancelOrder")) == 2 and out.stop_reason is None and states(db) == {A: "closed", B: "closed"}


def test_database_failure_after_placing_still_cancels_the_live_orders(db, monkeypatch):
    def locked(rows):
        raise OSError("database is locked")

    monkeypatch.setattr(db, "record_test_orders", locked)
    fake = FakeTradeSmart()
    out = run(db, fake, count=12)
    assert len(fake.sent("PlaceOrder")) == 8 and len(fake.sent("CancelOrder")) == 8     # stops after this burst
    assert "database" in out.stop_reason and db.get_run(out.run_id)["status"] != "running"


def test_failure_during_placement_still_journals_accepted_orders(db, monkeypatch):
    """Anything unexpected between orders: the accepted ones must stay findable."""
    real = ot.OrderRateGate.acquire
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("unexpected")
        return real(self)

    monkeypatch.setattr(ot.OrderRateGate, "acquire", flaky)
    out = run(db, FakeTradeSmart(), count=3)
    assert db.get_run(out.run_id)["status"] == "failed" and states(db) == {A: "open"}


# ---------------------------------------------------------------------------- reconcile: journal orders
def test_reconcile_cancels_open_journal_orders_and_closes_them(db):
    journal(db, A, B)
    fake = Book(histories={A: OPEN, B: OPEN})
    report = reconcile(db, fake)
    assert sorted(cancelled_numbers(fake)) == [A, B] and sorted(report.cancelled) == [A, B]
    assert report.checked == 2 and not report.needs_attention
    assert states(db) == {A: "closed", B: "closed"}


def test_reconcile_nothing_to_do(db):
    fake = Book()
    report = reconcile(db, fake)
    assert report.checked == 0 and not report.needs_attention and cancelled_numbers(fake) == []


def test_already_cancelled_is_closed_without_a_cancel(db):
    journal(db, A)
    fake = Book(histories={A: CANCELLED})
    report = reconcile(db, fake)
    assert cancelled_numbers(fake) == [] and report.closed == [A] and report.cancelled == []
    assert states(db) == {A: "closed"} and not report.needs_attention


def test_rejected_counts_as_closed(db):
    journal(db, A)
    report = reconcile(db, Book(histories={A: [{"status": "REJECTED"}]}))
    assert report.closed == [A] and states(db) == {A: "closed"}


def test_filled_is_recorded_and_needs_attention_without_a_cancel(db):
    journal(db, A)
    fake = Book(histories={A: [{"status": "COMPLETE", "fillshares": "1"}]})
    report = reconcile(db, fake)
    assert cancelled_numbers(fake) == [] and report.filled == [A] and report.needs_attention
    assert states(db) == {A: "filled"} and db.unresolved_test_orders() == []


def test_order_that_fills_while_being_cancelled_is_only_listed_as_filled(db):
    journal(db, A)
    fake = Book(histories={A: OPEN})
    fake.cancel_works = False
    real = fake.__call__

    def handler(request):
        response = real(request)
        if request.url.path.endswith("/CancelOrder"):
            fake.histories[A] = [{"status": "COMPLETE", "fillshares": "1"}]
        return response

    t = FakeTime()
    with TradeSmartClient(BASE, access_token="TOK", transport=httpx.MockTransport(handler)) as client:
        report = reconcile_test_orders(db, EPS, client, client_id="AB", sleep=t.sleep, clock=t.clock)
    assert report.filled == [A] and report.cancelled == [] and states(db) == {A: "filled"}


def test_cancel_failure_stays_unresolved(db):
    journal(db, A)
    fake = Book(histories={A: OPEN}, cancel={"stat": "Not_Ok", "emsg": "Order locked"})
    fake.cancel_works = False
    report = reconcile(db, fake)
    assert report.still_open == [A] and report.cancelled == [] and report.needs_attention
    assert any(A in e and "Order locked" in e for e in report.errors)
    assert states(db) == {A: "open"} and [r["norenordno"] for r in db.unresolved_test_orders()] == [A]


def test_cancel_refused_because_it_had_just_closed_is_not_an_error(db):
    journal(db, A)
    replies = iter([OPEN, CANCELLED])
    fake = Book(cancel={"stat": "Not_Ok", "emsg": "already closed"})
    fake.cancel_works = False
    fake.histories = {A: OPEN}
    real = fake.__call__

    def handler(request):
        if request.url.path.endswith("/SingleOrdHist"):
            fake.histories[A] = next(replies, CANCELLED)
        return real(request)

    t = FakeTime()
    with TradeSmartClient(BASE, access_token="TOK", transport=httpx.MockTransport(handler)) as client:
        report = reconcile_test_orders(db, EPS, client, client_id="AB", sleep=t.sleep, clock=t.clock)
    assert report.closed == [A] and report.errors == [] and not report.needs_attention


def test_accepted_cancel_that_never_closes_is_still_open_not_cancelled(db):
    journal(db, A)
    fake = Book(histories={A: OPEN})
    fake.cancel_works = False
    report = reconcile(db, fake)
    assert len(fake.sent("CancelOrder")) == 1                   # the fake says Ok but the status stays OPEN
    assert report.cancelled == [] and report.still_open == [A] and states(db) == {A: "open"}


def test_unreadable_status_never_cancels_blindly(db):
    journal(db, A)
    fake = Book(histories={A: None})
    report = reconcile(db, fake)
    assert cancelled_numbers(fake) == []
    assert report.still_open == [A] and report.needs_attention and states(db) == {A: "open"}


@pytest.mark.parametrize("history", [[], {"stat": "Ok"}, [{"fillshares": "0"}], {"stat": "Not_Ok", "emsg": "no data"}])
def test_history_without_a_status_never_cancels(db, history):
    journal(db, A)
    fake = Book(histories={A: history})
    report = reconcile(db, fake)
    assert cancelled_numbers(fake) == [] and report.still_open == [A] and states(db) == {A: "open"}


def test_status_lost_after_the_cancel_stays_unresolved(db):
    journal(db, A)
    fake = Book(histories={A: OPEN})
    fake.cancel_works = False
    real = fake.__call__

    def handler(request):
        response = real(request)
        if request.url.path.endswith("/CancelOrder"):
            fake.histories[A] = None
        return response

    t = FakeTime()
    with TradeSmartClient(BASE, access_token="TOK", transport=httpx.MockTransport(handler)) as client:
        report = reconcile_test_orders(db, EPS, client, client_id="AB", sleep=t.sleep, clock=t.clock)
    assert report.still_open == [A] and report.cancelled == [] and states(db) == {A: "open"}


# ---------------------------------------------------------------------------- reconcile: the order book
def test_stray_tagged_order_is_cancelled_and_journaled(db):
    fake = Book(book=[row(A)], histories={A: OPEN})
    report = reconcile(db, fake)
    assert cancelled_numbers(fake) == [A] and report.cancelled == [A] and states(db) == {A: "closed"}


def test_order_with_no_reply_is_recovered_from_the_book_by_its_tag(db):
    """A placement that timed out has no order number, so the journal can't know it. The tag finds it."""
    def boom(n):
        raise httpx.ReadTimeout("timed out")

    run(db, FakeTradeSmart(place=boom), count=1)
    assert states(db) == {}
    fake = Book(book=[row(A)], histories={A: OPEN})
    assert reconcile(db, fake).cancelled == [A]


@pytest.mark.parametrize("remarks", ["", None, "my own order", "latency-test-2", "LATENCY-TEST"])
def test_untagged_open_order_not_in_the_journal_is_never_cancelled(db, remarks):
    """Critical: this is somebody's real order."""
    fake = Book(book=[row(B, remarks=remarks)], histories={B: OPEN})
    report = reconcile(db, fake)
    assert cancelled_numbers(fake) == [] and fake.sent("SingleOrdHist") == []
    assert report.checked == 0 and states(db) == {} and not report.needs_attention


def test_order_without_remarks_field_is_never_cancelled(db):
    fake = Book(book=[{"norenordno": B, "status": "OPEN"}], histories={B: OPEN})
    reconcile(db, fake)
    assert cancelled_numbers(fake) == [] and states(db) == {}


def test_real_orders_are_left_alone_next_to_test_orders(db):
    journal(db, A)
    stray = "25101900000003"
    real_order = "25101900000009"
    fake = Book(book=[row(stray), row(real_order, remarks="swing trade"), row(A)],
                histories={A: OPEN, stray: OPEN, real_order: OPEN})
    report = reconcile(db, fake)
    assert sorted(cancelled_numbers(fake)) == [A, stray] and real_order not in states(db)
    assert report.checked == 2


@pytest.mark.parametrize("status", ["CANCELED", "REJECTED", "COMPLETE"])
def test_tagged_order_already_finished_is_ignored(db, status):
    fake = Book(book=[row(A, status=status)], histories={A: OPEN})
    reconcile(db, fake)
    assert cancelled_numbers(fake) == [] and states(db) == {}


@pytest.mark.parametrize("book", [
    {"stat": "Not_Ok", "emsg": "Error Occurred : 5 \"no data\""},
    [],
])
def test_empty_order_book_is_not_a_problem(db, book):
    report = reconcile(db, Book(book=book))
    assert report.errors == [] and not report.needs_attention


@pytest.mark.parametrize("book", [
    {"stat": "Not_Ok", "emsg": "Session Expired"},
    httpx.Response(500, text="oops"),
    httpx.Response(200, text="not json"),
])
def test_unreadable_order_book_is_reported_without_crashing(db, book):
    report = reconcile(db, Book(book=book))
    assert report.needs_attention and "order book" in report.errors[0] and report.checked == 0


def test_journal_orders_are_still_handled_when_the_book_is_unreadable(db):
    journal(db, A)
    fake = Book(book={"stat": "Not_Ok", "emsg": "Session Expired"}, histories={A: OPEN})
    report = reconcile(db, fake)
    assert report.cancelled == [A] and report.needs_attention


def test_junk_rows_in_the_book_are_skipped(db):
    fake = Book(book=["x", None, {"status": "OPEN", "remarks": TEST_ORDER_REMARK}, row(A)], histories={A: OPEN})
    assert reconcile(db, fake).cancelled == [A]


def test_cancels_pass_through_the_rate_gate(db, monkeypatch):
    counted = []
    real = ot.OrderRateGate.acquire
    monkeypatch.setattr(ot.OrderRateGate, "acquire", lambda self: counted.append(1) or real(self))
    journal(db, A, B)
    reconcile(db, Book(histories={A: OPEN, B: OPEN}))
    assert len(counted) == 2


def test_reconcile_refused_while_a_run_holds_the_lock(db):
    journal(db, A)
    fake = Book(histories={A: OPEN})
    with run_lock.exclusive_run(run_lock.lock_path_for(db.path), owner="alice", kind="benchmark"):
        with pytest.raises(OrderTestError, match="alice"):
            reconcile(db, fake)
    assert fake.calls == [] and states(db) == {A: "open"}


def test_reconcile_refuses_without_the_endpoints_it_needs(db):
    fake = Book()
    with TradeSmartClient(BASE, access_token="TOK", transport=httpx.MockTransport(fake)) as client:
        with pytest.raises(OrderTestError, match="OrderBook"):
            reconcile_test_orders(db, ORDER_EPS, client, client_id="AB")
    assert fake.calls == []


# ---------------------------------------------------------------------------- guards around a batch
def test_ip_guard_refusal_sends_nothing_and_creates_no_run(db):
    fake = FakeTradeSmart()
    with pytest.raises(OrderTestError, match="wrong IP"):
        run(db, fake, count=1, ip_guard=lambda: "wrong IP")
    assert fake.calls == [] and db.list_runs().empty
    assert run_lock.current_run(run_lock.lock_path_for(db.path)) is None        # lock released


def test_ip_guard_passing_allows_the_batch(db):
    fake = FakeTradeSmart()
    assert run(db, fake, count=1, ip_guard=lambda: None).stop_reason is None and fake.sent("PlaceOrder")


def test_ip_guard_that_crashes_sends_nothing(db):
    def boom():
        raise RuntimeError("guard failed")

    fake = FakeTradeSmart()
    with pytest.raises(RuntimeError):
        run(db, fake, count=1, ip_guard=boom)
    assert fake.calls == [] and db.list_runs().empty


def test_busy_lock_refuses_and_sends_nothing(db):
    fake = FakeTradeSmart()
    with run_lock.exclusive_run(run_lock.lock_path_for(db.path), owner="alice", kind="benchmark"):
        with pytest.raises(OrderTestError, match="alice"):
            run(db, fake, count=1)
    assert fake.calls == [] and db.list_runs().empty


def test_started_by_is_recorded_in_the_run_config(db):
    out = run(db, FakeTradeSmart(), count=1, started_by="alice")
    assert out.started_by == "alice" and db.get_run(out.run_id)["config"]["started_by"] == "alice"


def test_lock_is_held_during_a_batch_and_released_after(db):
    seen = []
    path = run_lock.lock_path_for(db.path)
    run(db, FakeTradeSmart(), count=1, started_by="alice", on_attempt=lambda a: seen.append(run_lock.current_run(path)))
    assert seen[0]["owner"] == "alice" and seen[0]["kind"] == "order test"
    assert run_lock.current_run(path) is None


def test_stop_event_passed_by_keyword_reaches_the_batch(db):
    stop = threading.Event()
    stop.set()
    fake = FakeTradeSmart()
    out = run(db, fake, count=2, stop_event=stop)
    assert fake.sent("PlaceOrder") == [] and out.stopped_by_user


def test_active_batch_is_registered_only_while_running(db):
    seen = []
    run(db, FakeTradeSmart(), count=1, on_attempt=lambda a: seen.append(list(ot._active_batches)))
    assert seen[0][0][1] is threading.current_thread() and ot._active_batches == []


def test_shutdown_hook_stops_running_batches(monkeypatch):
    stop = threading.Event()
    release = threading.Event()
    worker = threading.Thread(target=lambda: (stop.wait(10), release.set()))
    worker.start()
    monkeypatch.setattr(ot, "_active_batches", [(stop, worker), (threading.Event(), threading.current_thread())])
    ot._stop_batches_on_shutdown()                               # must not try to join itself
    assert stop.is_set() and release.is_set() and not worker.is_alive()


def test_shutdown_hook_gives_up_after_the_wait(monkeypatch):
    never = threading.Event()
    worker = threading.Thread(target=lambda: never.wait(10), daemon=True)
    worker.start()
    monkeypatch.setattr(ot, "_active_batches", [(threading.Event(), worker)])
    monkeypatch.setattr(ot, "SHUTDOWN_WAIT_S", 0.01)
    ot._stop_batches_on_shutdown()
    never.set()
    worker.join(5)


# ---------------------------------------------------------------------------- generic benchmark and the lock
def _runner(db, handler=None):
    transport = httpx.MockTransport(handler or (lambda r: httpx.Response(200, json={"stat": "Ok"})))
    return BenchmarkRunner(db, list(EPS.values()), lambda c: TradeSmartClient(BASE, "TOK", transport=transport),
                           BASE, sleep=lambda s: None)


def _config(**kw):
    return BenchmarkConfig(endpoint_ids=["GetQuotes"], iterations=1, warmup=0, delay_ms=0, environment="mock",
                           payload_context={"ClientId": "AB1", "exch": "NSE", "token": "1"}, **kw)


def test_benchmark_is_busy_while_an_order_batch_holds_the_lock(db):
    sent = []
    errors = []

    def during(attempt):
        try:
            _runner(db, lambda r: sent.append(r) or httpx.Response(200, json={"stat": "Ok"})).run(_config())
        except run_lock.RunBusyError as exc:
            errors.append(str(exc))

    run(db, FakeTradeSmart(), count=1, started_by="alice", on_attempt=during)
    assert errors and "alice" in errors[0] and sent == []


def test_order_batch_is_refused_while_a_benchmark_holds_the_lock(db):
    fake = FakeTradeSmart()
    t = FakeTime()

    def handler(request):
        with TradeSmartClient(BASE, access_token="TOK", transport=httpx.MockTransport(fake)) as client:
            with pytest.raises(OrderTestError, match="bob"):
                ot.run_order_test(db, EPS, client, client_id="AB1234", spec=SPEC, count=1,
                                  confirmation=confirmation_phrase(1), run_id="o", environment="live", base_url=BASE,
                                  timeout_s=10, sleep=t.sleep, clock=t.clock, now=NOW)
        return httpx.Response(200, json={"stat": "Ok"})

    _runner(db, handler).run(_config(started_by="bob"))
    assert fake.calls == []


def test_benchmark_started_by_lands_in_the_stored_config(db):
    rid = _runner(db).run(_config(started_by="alice"))
    assert db.get_run(rid)["config"]["started_by"] == "alice"
    assert run_lock.current_run(run_lock.lock_path_for(db.path)) is None


def test_benchmark_invalid_config_raises_before_locking(db):
    with pytest.raises(ValueError):
        _runner(db).run(BenchmarkConfig(endpoint_ids=[]))
    assert not run_lock.lock_path_for(db.path).exists()


def test_no_reply_blocks_next_batch_until_the_check_reads_the_book(db):
    def boom(n):
        raise httpx.ReadTimeout("timed out")

    run(db, FakeTradeSmart(place=boom), count=1)
    assert db.get_setting(ot.NEEDS_CHECK_SETTING) is True
    fake = FakeTradeSmart()
    with pytest.raises(OrderTestError, match="no reply"):
        run(db, fake, count=1)
    assert fake.calls == []

    # An unreadable book proves nothing, so the block stays.
    reconcile(db, Book(book={"stat": "Not_Ok", "emsg": "Session Expired"}))
    with pytest.raises(OrderTestError, match="no reply"):
        run(db, FakeTradeSmart(), count=1)

    reconcile(db, Book(book=[]))
    assert not db.get_setting(ot.NEEDS_CHECK_SETTING)
    assert run(db, FakeTradeSmart(), count=1).stop_reason is None
