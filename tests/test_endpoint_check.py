"""Tests for src.endpoint_check (single-endpoint check). Uses httpx.MockTransport; never hits the network."""
from __future__ import annotations

import json

import httpx
import pytest

from src.api_client import TradeSmartClient
from src.database import Database
from src.endpoint_check import (
    MAX_CHECK_REQUESTS, PREVIEW_MAX_ITEMS, preview_body, run_check, save_check,
)
from src.endpoint_registry import Endpoint
from src.redaction import REDACTED
from src.safety import UnsafeEndpointError

BASE = "https://mock.tradesmart.test/NorenWClientAPIv2"
READ = Endpoint(id="Limits", name="Limits", category="Holdings & Limits", path="/Limits",
                request_body_template={"uid": "{{ClientId}}"})
ORDER = Endpoint(id="PlaceOrder", name="Place Order", category="Orders", path="/PlaceOrder",
                 safety_class="state_changing", benchmark_enabled=False)


class FakeTime:
    """Deterministic clock whose sleep advances time (keeps the rate limiter instant in tests)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


def client_for(handler) -> TradeSmartClient:
    return TradeSmartClient(BASE, access_token="TOKEN-123", transport=httpx.MockTransport(handler))


def check(ep, handler, **kw):
    t = FakeTime()
    with client_for(handler) as client:
        return run_check(ep, {"uid": "AB1234"}, client, run_id="chk-1", sleep=t.sleep, clock=t.clock, **kw), t


def test_check_sends_requested_count_and_previews_redacted_body():
    calls = []

    def handler(request):
        calls.append(request.content.decode())
        return httpx.Response(200, json={"stat": "Ok", "actid": "AB1234", "cash": "1000.00"})

    outcome, t = check(READ, handler, requests=3, delay_ms=250)
    assert len(calls) == 3 and len(outcome.results) == 3
    assert all(r.success for r in outcome.results)
    assert [r.seq for r in outcome.results] == [1, 2, 3]
    assert sum(t.slept) >= 0.5 - 1e-9          # delay enforced between the 3 requests
    assert "AB1234" not in outcome.preview and REDACTED in outcome.preview
    assert '"cash": "1000.00"' in outcome.preview


def test_order_endpoint_is_blocked_before_anything_is_sent():
    def handler(request):
        pytest.fail("a state-changing endpoint must never be sent")

    with pytest.raises(UnsafeEndpointError):
        check(ORDER, handler, environment="live")
    with pytest.raises(UnsafeEndpointError):      # also blocked outside live without test mode
        check(ORDER, handler, environment="mock")


def test_request_count_is_capped():
    outcome, _ = check(READ, lambda r: httpx.Response(200, json={"stat": "Ok"}), requests=999, delay_ms=0)
    assert len(outcome.results) == MAX_CHECK_REQUESTS


def test_stops_on_rate_limit_without_retry():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={"stat": "Not_Ok", "emsg": "Too many requests"})

    outcome, _ = check(READ, handler, requests=5, delay_ms=0)
    assert len(calls) == 1 and outcome.results[0].outcome == "rate_limited"
    assert "429" in (outcome.stop_reason or "")


def test_api_error_is_reported_as_failure():
    outcome, _ = check(READ, lambda r: httpx.Response(200, json={"stat": "Not_Ok", "emsg": "Session Expired"}),
                       requests=1)
    r = outcome.results[0]
    assert not r.success and r.outcome == "api_error" and "Session Expired" in (r.error_message or "")


@pytest.mark.parametrize("body,expect_text,expect_note", [
    (b"", None, "Empty"),
    (b"<html>gateway error</html>", "gateway error", "not JSON"),
    (json.dumps([{"i": i} for i in range(PREVIEW_MAX_ITEMS + 3)]).encode(), '"i": 0',
     f"Showing {PREVIEW_MAX_ITEMS} of {PREVIEW_MAX_ITEMS + 3}"),
])
def test_preview_body_shapes(body, expect_text, expect_note):
    text, note = preview_body(body)
    assert expect_note in note
    if expect_text is None:
        assert text is None
    else:
        assert expect_text in text


def test_preview_redacts_registered_secret_strings():
    text, _ = preview_body(b'{"stat":"Ok","msg":"hello SECRETVALUE99"}', ["SECRETVALUE99"])
    assert "SECRETVALUE99" not in text


def test_save_check_creates_a_regular_http_run(tmp_path):
    db = Database(tmp_path / "t.db")
    outcome, _ = check(READ, lambda r: httpx.Response(200, json={"stat": "Ok"}), requests=4, delay_ms=0)
    run_id = save_check(db, READ, outcome, base_url=BASE, connection_mode="warm", timeout_s=10.0,
                        delay_ms=0, environment="live")
    run = db.get_run(run_id)
    assert run["run_type"] == "http" and run["status"] == "completed" and not run["is_synthetic"]
    assert run["config"]["source"] == "endpoint_check"
    assert len(db.get_results([run_id])) == 4
    m = db.get_metrics([run_id])
    assert int(m.iloc[0]["samples"]) == 4 and m.iloc[0]["endpoint_id"] == "Limits"
