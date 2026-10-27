"""Safety guards: endpoint classification, execution guard, rate limiting and circuit breaking.

Hard rules (see docs/TradeSmart_API_Latency_Benchmarking_Prompt.md section 5):

* Order placement / modification / cancellation / exit endpoints and the one-time ``GenAcsTok`` exchange
  are *always* treated as state-changing, whatever the registry JSON says.
* State-changing endpoints can never execute against the ``live`` environment. In ``mock``/``uat`` they
  additionally need an explicit state-changing test mode **and** user confirmation.
* Requests are paced well below documented limits (``safety_factor``) and endpoints without documented
  limits are capped conservatively (:data:`DEFAULT_UNDOCUMENTED_PER_SECOND`).
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from src.endpoint_registry import Endpoint

READ_ONLY, STATE_CHANGING, SPECIAL_SETUP = "read_only", "state_changing", "special_setup"

NEVER_AUTO_EXECUTE: frozenset[str] = frozenset(
    {"PlaceOrder", "ModifyOrder", "CancelOrder", "ExitSnoOrder", "ExitSNOOrder", "PlaceMultiOrder",
     "GenAcsTok"}
)
_NEVER_AUTO_LOWER: frozenset[str] = frozenset(name.lower() for name in NEVER_AUTO_EXECUTE)
# Any endpoint id containing one of these words is treated as order-mutating.
_STATE_CHANGING_WORDS: tuple[str, ...] = ("place", "modify", "cancel", "exit")

SAFE_TEST_ENVIRONMENTS: frozenset[str] = frozenset({"mock", "uat"})
DEFAULT_UNDOCUMENTED_PER_SECOND = 5  # conservative cap when docs give no limit


class UnsafeEndpointError(RuntimeError):
    """Raised when an endpoint must not be executed under the current settings."""


def is_never_auto_execute(endpoint_id: str) -> bool:
    """True for ids in :data:`NEVER_AUTO_EXECUTE` or containing Place/Modify/Cancel/Exit (case-insensitive)."""
    lowered = (endpoint_id or "").strip().lstrip("/").lower()
    if lowered in _NEVER_AUTO_LOWER:
        return True
    return any(word in lowered for word in _STATE_CHANGING_WORDS)


def _ep_never_auto(ep: "Endpoint") -> bool:
    return is_never_auto_execute(ep.id) or is_never_auto_execute(getattr(ep, "path", "") or "")


def classify(ep: "Endpoint") -> str:
    """Effective safety class: ``ep.safety_class`` but forced to STATE_CHANGING for never-auto names."""
    if _ep_never_auto(ep):
        return STATE_CHANGING
    if ep.safety_class not in (READ_ONLY, STATE_CHANGING, SPECIAL_SETUP):
        return STATE_CHANGING  # unknown classes are treated as the most restrictive
    return ep.safety_class


def is_auto_benchmarkable(ep: "Endpoint") -> bool:
    """True only for read-only endpoints that are not on the never-auto-execute list."""
    return classify(ep) == READ_ONLY and not _ep_never_auto(ep)


def assert_safe_to_execute(
    ep: "Endpoint",
    *,
    environment: str,
    state_changing_test_mode: bool = False,
    user_confirmed: bool = False,
) -> None:
    """Raise :class:`UnsafeEndpointError` unless ``ep`` may be executed under these conditions.

    read_only -> allowed. special_setup -> only with ``user_confirmed``. state_changing -> always refused
    in ``live``; otherwise requires environment in {mock, uat} AND test mode AND confirmation.
    """
    cls = classify(ep)
    env = (environment or "").strip().lower()
    if cls == READ_ONLY:
        return
    if cls == SPECIAL_SETUP:
        if not user_confirmed:
            raise UnsafeEndpointError(f"{ep.id} requires special setup and explicit user confirmation")
        return
    # STATE_CHANGING
    if env == "live":
        raise UnsafeEndpointError(
            f"{ep.id} is state-changing and can never be executed against the live environment"
        )
    if env not in SAFE_TEST_ENVIRONMENTS:
        raise UnsafeEndpointError(f"{ep.id} is state-changing; environment {environment!r} is not a safe test env")
    if not state_changing_test_mode:
        raise UnsafeEndpointError(f"{ep.id} is state-changing; enable the separate state-changing test mode")
    if not user_confirmed:
        raise UnsafeEndpointError(f"{ep.id} is state-changing; explicit user confirmation is required")


def live_account_warning(environment: str) -> str | None:
    """Warning text to show when the live environment is selected, else ``None``."""
    if (environment or "").strip().lower() == "live":
        return (
            "LIVE ACCOUNT selected: benchmarks send real requests to the TradeSmart production API using "
            "your credentials. Only read-only endpoints run automatically; order placement, modification "
            "and cancellation are never executed. Requests count toward your rate limits."
        )
    return None


# --------------------------------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------------------------------
class RateLimiter:
    """Sliding-window limiter for per-second and per-minute caps plus a minimum interval. Thread-safe.

    Effective limits are ``floor(limit * safety_factor)`` (at least 1). ``clock`` and ``sleep`` are
    injectable so tests can run deterministically without real waiting.
    """

    def __init__(
        self,
        per_second: float | None,
        per_minute: float | None,
        safety_factor: float = 0.5,
        min_interval_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 0 < safety_factor <= 1:
            raise ValueError("safety_factor must be in (0, 1]")
        self.per_second = per_second
        self.per_minute = per_minute
        self.safety_factor = safety_factor
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._second: deque[float] = deque()
        self._minute: deque[float] = deque()
        self._last: float | None = None

    def _effective(self, limit: float | None) -> float | None:
        if limit is None or limit <= 0:
            return None
        return float(max(1, math.floor(limit * self.safety_factor)))

    @property
    def effective_per_second(self) -> float | None:
        """Per-second cap after the safety factor (None = no per-second cap)."""
        return self._effective(self.per_second)

    @property
    def effective_per_minute(self) -> float | None:
        """Per-minute cap after the safety factor (None = no per-minute cap)."""
        return self._effective(self.per_minute)

    def _required_wait(self, now: float) -> float:
        while self._second and now - self._second[0] >= 1.0:
            self._second.popleft()
        while self._minute and now - self._minute[0] >= 60.0:
            self._minute.popleft()
        wait = 0.0
        eps = self.effective_per_second
        if eps is not None and len(self._second) >= eps:
            wait = max(wait, self._second[0] + 1.0 - now)
        epm = self.effective_per_minute
        if epm is not None and len(self._minute) >= epm:
            wait = max(wait, self._minute[0] + 60.0 - now)
        if self._last is not None and self.min_interval_s > 0:
            wait = max(wait, self._last + self.min_interval_s - now)
        return wait

    def acquire(self) -> float:
        """Block until a request is allowed, record it, and return the seconds spent waiting."""
        waited = 0.0
        with self._lock:
            while True:
                now = self._clock()
                wait = self._required_wait(now)
                if wait <= 1e-9:
                    self._second.append(now)
                    self._minute.append(now)
                    self._last = now
                    return waited
                self._sleep(wait)
                waited += wait


def rate_limiter_for(
    ep: "Endpoint", safety_factor: float = 0.5, min_interval_s: float = 0.0, **kw: Any
) -> RateLimiter:
    """Build a limiter from the endpoint's documented limits.

    Without any documented limit the endpoint is capped at :data:`DEFAULT_UNDOCUMENTED_PER_SECOND`
    requests/second (already conservative, so the safety factor is not applied on top of it).
    ``kw`` is forwarded (``clock``, ``sleep``).
    """
    per_sec, per_min = ep.rate_limit_per_sec, ep.rate_limit_per_min
    if per_sec is None and per_min is None:
        return RateLimiter(DEFAULT_UNDOCUMENTED_PER_SECOND, None, safety_factor=1.0,
                           min_interval_s=min_interval_s, **kw)
    return RateLimiter(per_sec, per_min, safety_factor=safety_factor, min_interval_s=min_interval_s, **kw)


# --------------------------------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------------------------------
class CircuitBreaker:
    """Per-endpoint breaker: trips after N consecutive failures or on a rate-limit response."""

    def __init__(self, max_consecutive_failures: int = 5, stop_on_rate_limit: bool = True) -> None:
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        self.stop_on_rate_limit = stop_on_rate_limit
        self.consecutive_failures = 0
        self.rate_limited_seen = False
        self._reason: str | None = None
        self._lock = threading.Lock()

    def record(self, outcome: str) -> None:
        """Record a request outcome (one of ``api_client.OUTCOMES``)."""
        with self._lock:
            if outcome == "success":
                self.consecutive_failures = 0
                return
            self.consecutive_failures += 1
            if outcome == "rate_limited":
                self.rate_limited_seen = True
                if self._reason is None:
                    self._reason = "rate limited (HTTP 429)"
            if self._reason is None and self.consecutive_failures >= self.max_consecutive_failures:
                self._reason = f"{self.consecutive_failures} consecutive failures (last: {outcome})"

    @property
    def tripped(self) -> bool:
        """True once the endpoint should be paused."""
        return self._reason is not None

    @property
    def reason(self) -> str | None:
        """Human-readable reason the breaker tripped, if it did."""
        return self._reason

    @property
    def global_stop(self) -> bool:
        """True when a rate-limit response was seen and ``stop_on_rate_limit`` is enabled."""
        return self.rate_limited_seen and self.stop_on_rate_limit
