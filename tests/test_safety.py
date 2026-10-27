"""Tests for src.safety: classification, execution guard, rate limiter and circuit breaker."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from src.endpoint_registry import Endpoint, load_endpoints
from src.safety import (
    DEFAULT_UNDOCUMENTED_PER_SECOND,
    READ_ONLY,
    SPECIAL_SETUP,
    STATE_CHANGING,
    CircuitBreaker,
    RateLimiter,
    UnsafeEndpointError,
    assert_safe_to_execute,
    classify,
    is_auto_benchmarkable,
    live_account_warning,
    rate_limiter_for,
)

FIXTURE = Path(__file__).parent / "fixtures" / "endpoints_sample.json"


class FakeClock:
    """Deterministic clock whose sleep advances time instantly."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def eps() -> dict[str, Endpoint]:
    return {e.id: e for e in load_endpoints(FIXTURE)}


def ep(ep_id: str, safety_class: str = "read_only", **kw) -> Endpoint:
    return Endpoint(id=ep_id, name=ep_id, category="x", path=f"/{ep_id}", safety_class=safety_class,
                    benchmark_enabled=safety_class != "state_changing", **kw)


def test_classify_and_forced_state_changing(eps):
    assert classify(eps["UserDetails"]) == READ_ONLY
    assert classify(eps["PlaceOrder"]) == STATE_CHANGING
    # never-auto names are forced to state_changing even if JSON claims read_only
    for name in ("PlaceOrder", "ModifyOrder", "CancelOrder", "ExitSnoOrder", "ExitSNOOrder", "PlaceMultiOrder",
                 "GenAcsTok", "placeorder", "SomethingCancelX"):
        assert classify(ep(name)) == STATE_CHANGING, name
        assert not is_auto_benchmarkable(ep(name))
    assert classify(ep("GetOrderMargin")) == READ_ONLY
    assert classify(ep("Foo", "special_setup")) == SPECIAL_SETUP
    assert is_auto_benchmarkable(eps["GetQuotes"])
    assert not is_auto_benchmarkable(ep("Foo", "special_setup"))


def test_state_changing_never_in_live(eps):
    with pytest.raises(UnsafeEndpointError, match="live"):
        assert_safe_to_execute(eps["PlaceOrder"], environment="live", state_changing_test_mode=True,
                               user_confirmed=True)


def test_state_changing_requires_mode_and_confirmation(eps):
    po = eps["PlaceOrder"]
    for mode, confirmed in ((False, False), (True, False), (False, True)):
        with pytest.raises(UnsafeEndpointError):
            assert_safe_to_execute(po, environment="mock", state_changing_test_mode=mode, user_confirmed=confirmed)
    assert_safe_to_execute(po, environment="mock", state_changing_test_mode=True, user_confirmed=True)
    assert_safe_to_execute(po, environment="uat", state_changing_test_mode=True, user_confirmed=True)
    with pytest.raises(UnsafeEndpointError):
        assert_safe_to_execute(po, environment="staging", state_changing_test_mode=True, user_confirmed=True)


def test_read_only_and_special_setup(eps):
    assert_safe_to_execute(eps["UserDetails"], environment="live")
    special = ep("Fixture", "special_setup")
    with pytest.raises(UnsafeEndpointError):
        assert_safe_to_execute(special, environment="live")
    assert_safe_to_execute(special, environment="live", user_confirmed=True)


def test_live_warning():
    assert "LIVE" in (live_account_warning("live") or "")
    assert live_account_warning("mock") is None


def test_effective_limits():
    rl = RateLimiter(100, 1000, safety_factor=0.5)
    assert rl.effective_per_second == 50 and rl.effective_per_minute == 500
    assert RateLimiter(1, None, safety_factor=0.5).effective_per_second == 1
    assert RateLimiter(None, None).effective_per_second is None


def test_rate_limiter_limits_within_fake_second():
    clock = FakeClock()
    rl = RateLimiter(10, None, safety_factor=0.5, clock=clock, sleep=clock.sleep)  # 5/s effective
    start = clock.now
    for _ in range(5):
        assert rl.acquire() == 0.0
    assert clock.now == start  # first 5 within the same instant
    waited = rl.acquire()  # 6th must wait for the window to slide
    assert waited == pytest.approx(1.0)
    assert clock.now - start == pytest.approx(1.0)
    # 11 acquisitions take at least 2 seconds of (fake) time
    for _ in range(5):
        rl.acquire()
    assert clock.now - start >= 2.0 - 1e-9


def test_rate_limiter_per_minute_and_min_interval():
    clock = FakeClock()
    rl = RateLimiter(None, 4, safety_factor=0.5, clock=clock, sleep=clock.sleep)  # 2/min
    rl.acquire(); rl.acquire()
    assert rl.acquire() == pytest.approx(60.0)
    clock2 = FakeClock()
    rl2 = RateLimiter(None, None, min_interval_s=0.25, clock=clock2, sleep=clock2.sleep)
    rl2.acquire()
    assert rl2.acquire() == pytest.approx(0.25)


def test_rate_limiter_for_documented_and_undocumented(eps):
    clock = FakeClock()
    rl = rate_limiter_for(eps["GetQuotes"], clock=clock, sleep=clock.sleep)
    assert rl.effective_per_second == 50 and rl.effective_per_minute == 500
    rl2 = rate_limiter_for(eps["UserDetails"], clock=clock, sleep=clock.sleep)
    assert rl2.effective_per_second == DEFAULT_UNDOCUMENTED_PER_SECOND
    assert rl2.effective_per_minute is None


def test_rate_limiter_thread_safe():
    clock = FakeClock()
    lock = threading.Lock()

    def sleep(s: float) -> None:
        with lock:
            clock.now += s

    rl = RateLimiter(4, None, safety_factor=1.0, clock=clock, sleep=sleep)
    threads = [threading.Thread(target=rl.acquire) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(2)
    assert clock.now - 1000.0 == pytest.approx(1.0)  # 8 requests at 4/s -> one window slide


def test_circuit_breaker_consecutive_failures():
    cb = CircuitBreaker(max_consecutive_failures=3)
    cb.record("api_error"); cb.record("timeout"); cb.record("success")
    assert not cb.tripped
    for _ in range(3):
        cb.record("http_error")
    assert cb.tripped and "3 consecutive" in (cb.reason or "")
    assert not cb.global_stop


def test_circuit_breaker_rate_limit():
    cb = CircuitBreaker(max_consecutive_failures=5, stop_on_rate_limit=True)
    cb.record("rate_limited")
    assert cb.tripped and cb.global_stop
    cb2 = CircuitBreaker(stop_on_rate_limit=False)
    cb2.record("rate_limited")
    assert cb2.tripped and not cb2.global_stop
