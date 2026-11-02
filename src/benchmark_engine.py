"""Benchmark configuration, planning and execution.

* :class:`BenchmarkConfig` - user-selected test settings (validated, serialisable without secrets).
* :func:`build_plan` - planned request count, estimated duration, blocked endpoints, warnings.
* :class:`BenchmarkRunner` - executes warm-ups then measured iterations with pacing, rate limiting,
  circuit breaking, stop/max-duration handling, batched persistence and metrics.
* :func:`record_manual_result` - documentation-only / manual latency entry for order endpoints.

Safety: endpoints rejected by :func:`src.safety.assert_safe_to_execute` are never sent; they are recorded
in the plan's ``blocked`` list and in the run's notes / stop reason. No request is ever retried.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

import pandas as pd

from src import run_lock, safety
from src.api_client import CONNECTION_MODES, RequestResult, TradeSmartClient, utc_now_iso
from src.database import new_run_id
from src.endpoint_registry import Endpoint, missing_placeholders, render_payload
from src.metrics import endpoint_metrics
from src.redaction import mask_secret, redact_text

if TYPE_CHECKING:
    from src.database import Database

logger = logging.getLogger("tsbench")

ORDERS = ("round_robin", "grouped")
ENVIRONMENTS = ("live", "uat", "mock")
MAX_CONCURRENCY = 4
FLUSH_EVERY = 25
# payload_context keys whose values identify the account and must never be stored unmasked
SENSITIVE_CONTEXT_KEYS = frozenset({
    "clientid", "client_id", "uid", "actid", "accno", "bankaccno", "pan", "accesstoken", "access_token",
    "susertoken", "password", "pwd", "checksum", "code", "secret", "secret_key", "app_key", "apikey", "api_key",
})


def _mask_mapping(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            out[k] = _mask_mapping(v)
        elif str(k).lower() in SENSITIVE_CONTEXT_KEYS and v not in (None, ""):
            out[k] = mask_secret(str(v))
        else:
            out[k] = v
    return out


@dataclass
class BenchmarkConfig:
    """User-selected benchmark settings. See docs/ARCHITECTURE.md section 10."""

    endpoint_ids: list[str]
    iterations: int = 20
    warmup: int = 2
    timeout_s: float = 10.0
    delay_ms: int = 500
    connection_mode: str = "warm"
    order: str = "round_robin"
    concurrency: int = 1
    max_duration_s: float | None = None
    safety_factor: float = 0.5
    stop_on_rate_limit: bool = True
    max_consecutive_failures: int = 5
    payload_context: dict[str, Any] = field(default_factory=dict)
    payload_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    environment: str = "live"
    notes: str = ""
    started_by: str = "local"
    # Additions: separate opt-ins for state-changing endpoints (never effective when environment == "live").
    state_changing_test_mode: bool = False
    user_confirmed: bool = False

    def validate(self) -> list[str]:
        """Return a list of configuration problems (``[]`` = valid)."""
        p: list[str] = []
        if not self.endpoint_ids:
            p.append("select at least one endpoint")
        if len(set(self.endpoint_ids)) != len(self.endpoint_ids):
            p.append("endpoint_ids contains duplicates")
        if not 1 <= int(self.iterations) <= 1000:
            p.append("iterations must be between 1 and 1000")
        if not 0 <= int(self.warmup) <= 20:
            p.append("warmup must be between 0 and 20")
        if not 0 < float(self.timeout_s) <= 120:
            p.append("timeout_s must be > 0 and <= 120")
        if not 0 <= int(self.delay_ms) <= 60_000:
            p.append("delay_ms must be between 0 and 60000")
        if self.connection_mode not in CONNECTION_MODES:
            p.append(f"connection_mode must be one of {CONNECTION_MODES}")
        if self.order not in ORDERS:
            p.append(f"order must be one of {ORDERS}")
        if not 1 <= int(self.concurrency) <= MAX_CONCURRENCY:
            p.append(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
        if self.max_duration_s is not None and float(self.max_duration_s) <= 0:
            p.append("max_duration_s must be positive (or empty for no limit)")
        if not 0 < float(self.safety_factor) <= 1:
            p.append("safety_factor must be in (0, 1]")
        if int(self.max_consecutive_failures) < 1:
            p.append("max_consecutive_failures must be >= 1")
        if self.environment not in ENVIRONMENTS:
            p.append(f"environment must be one of {ENVIRONMENTS}")
        return p

    def to_dict(self) -> dict:
        """Serialisable dict without secrets (account identifiers in payload context/overrides masked)."""
        d = asdict(self)
        d["payload_context"] = _mask_mapping(self.payload_context)
        d["payload_overrides"] = {k: _mask_mapping(v) for k, v in (self.payload_overrides or {}).items()}
        d["notes"] = redact_text(self.notes) if self.notes else ""
        return d


@dataclass
class BenchmarkPlan:
    """What a run will do, shown to the user before starting."""

    total_requests: int
    warmup_requests: int
    measured_requests: int
    estimated_duration_s: float
    per_endpoint: list[dict]
    blocked: list[dict]
    warnings: list[str]

    @property
    def runnable_ids(self) -> list[str]:
        """Endpoint ids that will actually be executed, in configured order."""
        return [p["endpoint_id"] for p in self.per_endpoint]


def build_plan(config: BenchmarkConfig, endpoints: list[Endpoint], typical_latency_ms: float = 150.0) -> BenchmarkPlan:
    """Plan a run: per-endpoint request counts, blocked endpoints and an estimated duration.

    The estimate is ``max(sequential time, rate-limit bound)`` where sequential time is
    ``requests * (typical latency + delay) / concurrency``; it is capped at ``max_duration_s``.
    """
    by_id = {ep.id: ep for ep in endpoints}
    per_endpoint: list[dict] = []
    blocked: list[dict] = []
    warnings: list[str] = []
    per_ep_requests = max(0, int(config.warmup)) + max(0, int(config.iterations))

    for ep_id in config.endpoint_ids:
        ep = by_id.get(ep_id)
        if ep is None:
            blocked.append({"endpoint_id": ep_id, "reason": "unknown endpoint id", "missing": []})
            continue
        try:
            safety.assert_safe_to_execute(ep, environment=config.environment,
                                          state_changing_test_mode=config.state_changing_test_mode,
                                          user_confirmed=config.user_confirmed)
        except safety.UnsafeEndpointError as exc:
            blocked.append({"endpoint_id": ep_id, "reason": f"unsafe: {exc}", "missing": []})
            continue
        missing = missing_placeholders(ep, config.payload_context, config.payload_overrides.get(ep_id))
        if missing:
            blocked.append({"endpoint_id": ep_id, "reason": f"missing placeholders: {', '.join(missing)}",
                            "missing": missing})
            continue
        limiter = safety.rate_limiter_for(ep, safety_factor=config.safety_factor)
        eps, epm = limiter.effective_per_second, limiter.effective_per_minute
        rate_bound = 0.0
        if eps:
            rate_bound = max(rate_bound, (per_ep_requests - 1) / eps)
        if epm:
            rate_bound = max(rate_bound, ((per_ep_requests - 1) // int(epm)) * 60.0)
        per_endpoint.append({
            "endpoint_id": ep_id, "endpoint_name": ep.name, "requests": per_ep_requests,
            "warmup": int(config.warmup), "iterations": int(config.iterations),
            "effective_rate_limit": eps, "effective_per_minute": epm,
            "documented_per_second": ep.rate_limit_per_sec, "documented_per_minute": ep.rate_limit_per_min,
            "rate_limit_documented": ep.rate_limit_per_sec is not None or ep.rate_limit_per_min is not None,
            "rate_bound_s": rate_bound,
        })

    n_eps = len(per_endpoint)
    warmup_requests = n_eps * int(config.warmup)
    measured_requests = n_eps * int(config.iterations)
    total = warmup_requests + measured_requests
    sequential = total * (typical_latency_ms + config.delay_ms) / 1000.0 / max(1, int(config.concurrency))
    rate_bound_total = max((p["rate_bound_s"] for p in per_endpoint), default=0.0)
    estimate = max(sequential, rate_bound_total)
    if config.max_duration_s is not None and estimate > config.max_duration_s:
        warnings.append(f"Estimated duration {estimate:.0f}s exceeds max duration {config.max_duration_s:.0f}s; "
                        "the run will stop early.")
        estimate = float(config.max_duration_s)

    live = safety.live_account_warning(config.environment)
    if live:
        warnings.append(live)
    if blocked:
        warnings.append(f"{len(blocked)} endpoint(s) blocked and will not be sent: "
                        + ", ".join(b["endpoint_id"] for b in blocked))
    if n_eps == 0:
        warnings.append("No runnable endpoints selected.")
    if config.iterations < 20:
        warnings.append(f"{config.iterations} iterations per endpoint: P95 needs >= 20 samples to be meaningful.")
    elif config.iterations < 100:
        warnings.append(f"{config.iterations} iterations per endpoint: P99 needs >= 100 samples to be meaningful.")
    if config.concurrency > 1:
        warnings.append(f"Concurrency {config.concurrency}: requests overlap, which can inflate latency and "
                        "approach rate limits. Sequential mode is recommended for comparisons.")
    if config.connection_mode == "cold":
        warnings.append("Cold mode: each request opens a new connection (DNS + TCP + TLS included).")
    undocumented = [p["endpoint_id"] for p in per_endpoint if not p["rate_limit_documented"]]
    if undocumented:
        warnings.append(f"No documented rate limit for {len(undocumented)} endpoint(s); capped at "
                        f"{safety.DEFAULT_UNDOCUMENTED_PER_SECOND} requests/second.")
    return BenchmarkPlan(total_requests=total, warmup_requests=warmup_requests,
                         measured_requests=measured_requests, estimated_duration_s=round(estimate, 2),
                         per_endpoint=per_endpoint, blocked=blocked, warnings=warnings)


@dataclass
class Progress:
    """Progress snapshot passed to the runner's callback after each request."""

    run_id: str
    completed: int
    total: int
    current_endpoint: str | None
    last_result: RequestResult | None
    elapsed_s: float
    message: str = ""


class _StopRequested(Exception):
    """Internal: raised inside a rate-limiter wait when the stop event is set."""


def _schedule(config: BenchmarkConfig, endpoints: list[Endpoint]) -> list[tuple[Endpoint, int, bool]]:
    """Ordered list of (endpoint, seq, is_warmup). Warm-ups always precede measured iterations."""
    items: list[tuple[Endpoint, int, bool]] = []
    w, n = int(config.warmup), int(config.iterations)
    if config.order == "grouped":
        for ep in endpoints:
            items.extend((ep, i + 1, i < w) for i in range(w + n))
    else:  # round_robin: interleave endpoints per round
        for i in range(w + n):
            items.extend((ep, i + 1, i < w) for ep in endpoints)
    return items


class BenchmarkRunner:
    """Executes a :class:`BenchmarkConfig` and persists every request via ``db``.

    ``sleep``/``clock`` are optional injection points for tests (default: ``stop_event.wait`` for
    responsive pauses and ``time.monotonic``).
    """

    def __init__(
        self,
        db: "Database",
        endpoints: list[Endpoint],
        client_factory: Callable[[BenchmarkConfig], TradeSmartClient],
        base_url: str,
        logger: logging.Logger | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.endpoints = list(endpoints)
        self.client_factory = client_factory
        self.base_url = base_url
        self.log = logger or logging.getLogger("tsbench")
        self._sleep = sleep
        self._clock = clock

    # -- helpers ---------------------------------------------------------------------------------
    def _pause(self, seconds: float, stop_event: threading.Event) -> None:
        if seconds <= 0:
            return
        if self._sleep is not None:
            self._sleep(seconds)
        else:
            stop_event.wait(seconds)

    def run(
        self,
        config: BenchmarkConfig,
        progress_cb: Callable[[Progress], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> str:
        """Run the benchmark and return its run id.

        Raises ``ValueError`` for an invalid config and :class:`run_lock.RunBusyError` when another run holds
        the server-wide lock (nothing is sent in either case).
        """
        problems = config.validate()
        if problems:
            raise ValueError("invalid benchmark config: " + "; ".join(problems))
        db_path = getattr(self.db, "path", None)
        if db_path is None:  # test doubles without a database file
            return self._run(config, progress_cb, stop_event)
        with run_lock.exclusive_run(run_lock.lock_path_for(db_path), owner=config.started_by, kind="benchmark"):
            return self._run(config, progress_cb, stop_event)

    def _run(
        self,
        config: BenchmarkConfig,
        progress_cb: Callable[[Progress], None] | None,
        stop_event: threading.Event | None,
    ) -> str:
        stop_event = stop_event or threading.Event()
        plan = build_plan(config, self.endpoints)
        by_id = {ep.id: ep for ep in self.endpoints}
        runnable = [by_id[i] for i in plan.runnable_ids]
        blocked_text = "; ".join(f"{b['endpoint_id']} ({b['reason']})" for b in plan.blocked)

        run_id = new_run_id("run")
        notes = config.notes or ""
        if blocked_text:
            notes = (notes + "\n" if notes else "") + f"Blocked (not sent): {blocked_text}"
        cfg_dict = config.to_dict()
        cfg_dict["plan"] = {"total_requests": plan.total_requests,
                            "estimated_duration_s": plan.estimated_duration_s,
                            "blocked": [{"endpoint_id": b["endpoint_id"], "reason": b["reason"]} for b in plan.blocked]}
        self.db.create_run(run_id=run_id, run_type="http", environment=config.environment,
                           base_host=urlparse(self.base_url).hostname or "", connection_mode=config.connection_mode,
                           config=cfg_dict, planned_requests=plan.total_requests, notes=notes)
        self.log.info("Run %s started: %d requests across %d endpoint(s)", run_id, plan.total_requests, len(runnable))

        if not runnable:
            reason = "no runnable endpoints" + (f"; blocked: {blocked_text}" if blocked_text else "")
            self.db.finish_run(run_id, "failed", reason)
            return run_id

        payloads: dict[str, dict] = {}
        for ep in runnable:
            payloads[ep.id] = render_payload(ep, config.payload_context, config.payload_overrides.get(ep.id))

        def limiter_sleep(seconds: float) -> None:
            self._pause(seconds, stop_event)
            if stop_event.is_set():
                raise _StopRequested

        limiters = {ep.id: safety.rate_limiter_for(ep, safety_factor=config.safety_factor, clock=self._clock,
                                                   sleep=limiter_sleep) for ep in runnable}
        breakers = {ep.id: safety.CircuitBreaker(config.max_consecutive_failures, config.stop_on_rate_limit)
                    for ep in runnable}
        schedule = _schedule(config, runnable)
        total = len(schedule)
        all_results: list[RequestResult] = []
        pending: list[RequestResult] = []
        status, main_reason = "completed", None
        start = self._clock()
        completed = 0
        skipped = 0

        def flush() -> None:
            if pending:
                self.db.insert_results(list(pending))
                pending.clear()

        def execute(client: TradeSmartClient, ep: Endpoint, seq: int, warm: bool) -> RequestResult | None:
            try:
                limiters[ep.id].acquire()
            except _StopRequested:
                return None  # stop requested while waiting for the rate limiter: request never sent
            return client.timed_post(ep, payloads[ep.id], run_id=run_id, seq=seq, is_warmup=warm)

        def handle(result: RequestResult | None) -> None:
            nonlocal completed
            if result is None:
                return
            breakers[result.endpoint_id].record(result.outcome)
            all_results.append(result)
            pending.append(result)
            completed += 1
            if len(pending) >= FLUSH_EVERY:
                flush()
            if progress_cb is not None:
                try:
                    progress_cb(Progress(run_id, completed, total, result.endpoint_id, result,
                                         self._clock() - start,
                                         f"{result.endpoint_id} #{result.seq}{' (warm-up)' if result.is_warmup else ''}: "
                                         f"{result.outcome}"))
                except Exception:  # noqa: BLE001 - UI callback errors must not abort the run
                    self.log.exception("progress callback failed")

        client: TradeSmartClient | None = None
        try:
            client = self.client_factory(config)
            concurrency = max(1, min(MAX_CONCURRENCY, int(config.concurrency)))
            executor = ThreadPoolExecutor(max_workers=concurrency) if concurrency > 1 else None
            try:
                idx = 0
                first = True
                while idx < len(schedule):
                    if stop_event.is_set():
                        status, main_reason = "stopped", "stopped by user"
                        break
                    if config.max_duration_s is not None and self._clock() - start >= config.max_duration_s:
                        status, main_reason = "stopped", f"max duration {config.max_duration_s:g}s reached"
                        break
                    tripped_global = next((b for b in breakers.values() if b.global_stop), None)
                    if tripped_global is not None:
                        status, main_reason = "stopped", "rate limit response (HTTP 429) received; run stopped"
                        break
                    if all(b.tripped for b in breakers.values()):
                        status, main_reason = "stopped", "all endpoints paused by circuit breaker"
                        break
                    # take the next batch of non-paused items
                    batch: list[tuple[Endpoint, int, bool]] = []
                    while idx < len(schedule) and len(batch) < concurrency:
                        ep, seq, warm = schedule[idx]
                        idx += 1
                        if breakers[ep.id].tripped:
                            skipped += 1
                            continue
                        batch.append((ep, seq, warm))
                    if not batch:
                        continue
                    if not first:
                        self._pause(config.delay_ms / 1000.0, stop_event)
                        if stop_event.is_set():
                            status, main_reason = "stopped", "stopped by user"
                            break
                    first = False
                    if executor is None:
                        ep, seq, warm = batch[0]
                        handle(execute(client, ep, seq, warm))
                    else:
                        futures = [executor.submit(execute, client, ep, seq, warm) for ep, seq, warm in batch]
                        for fut in futures:
                            handle(fut.result())
            finally:
                if executor is not None:
                    executor.shutdown(wait=True)
        except Exception as exc:  # noqa: BLE001 - runner-level failure (not a request error)
            status = "failed"
            main_reason = redact_text(f"runner error: {type(exc).__name__}: {exc}")
            self.log.exception("Run %s failed", run_id)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                flush()
            except Exception as exc:  # noqa: BLE001
                status = "failed"
                main_reason = redact_text(f"could not store results: {exc}")

        reasons = [main_reason] if main_reason else []
        paused = [f"{ep_id} ({b.reason})" for ep_id, b in breakers.items() if b.tripped]
        if paused:
            reasons.append("paused: " + "; ".join(paused) + (f" ({skipped} request(s) skipped)" if skipped else ""))
        if blocked_text:
            reasons.append("blocked (not sent): " + blocked_text)
        self._save_metrics(run_id, all_results, by_id)
        self.db.finish_run(run_id, status, " | ".join(reasons) if reasons else None)
        self.log.info("Run %s finished: %s (%d/%d requests)", run_id, status, completed, total)
        return run_id

    def _save_metrics(self, run_id: str, results: list[RequestResult], by_id: dict[str, Endpoint]) -> None:
        if not results:
            return
        try:
            df = pd.DataFrame([r.to_row() for r in results])
            df["category"] = df["endpoint_id"].map(lambda i: by_id[i].category if i in by_id else None)
            self.db.save_endpoint_metrics(run_id, endpoint_metrics(df))
        except Exception:  # noqa: BLE001 - results are stored; metrics can be recomputed later
            self.log.exception("Could not compute/save metrics for run %s", run_id)


def record_manual_result(
    db: "Database",
    ep: Endpoint,
    latency_ms: float,
    http_status: int | None,
    success: bool,
    environment: str,
    notes: str = "",
    run_id: str | None = None,
) -> str:
    """Store a manually measured latency (e.g. order endpoints run outside this tool).

    Creates a ``run_type="manual"`` run unless ``run_id`` is given; the row has ``source="manual"``.
    Nothing is sent to the API. Returns the run id.
    """
    if latency_ms is None or float(latency_ms) < 0:
        raise ValueError("latency_ms must be a non-negative number")
    if run_id is None:
        run_id = new_run_id("manual")
        db.create_run(run_id=run_id, run_type="manual", environment=environment, base_host="",
                      connection_mode="manual",
                      config={"endpoint_id": ep.id, "manual": True, "safety_class": safety.classify(ep)},
                      planned_requests=1, notes=notes)
    if success:
        outcome = "success"
    elif http_status == 429:
        outcome = "rate_limited"
    elif http_status is not None and not 200 <= int(http_status) < 300:
        outcome = "http_error"
    else:
        outcome = "api_error"
    now = utc_now_iso()
    result = RequestResult(
        run_id=run_id, endpoint_id=ep.id, endpoint_name=ep.name, path=ep.url_path, method=ep.method, seq=1,
        is_warmup=False, connection_mode="manual", started_at=now, completed_at=now,
        latency_ms=float(latency_ms), http_status=http_status, success=bool(success), outcome=outcome,
        is_timeout=False, error_type=None if success else "manual",
        error_message=None if success else (redact_text(notes) or "recorded as failed"),
        response_bytes=0, api_stat=None, server_timing=None, source="manual",
    )
    db.insert_results([result])
    try:
        db.recompute_metrics(run_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not recompute metrics for manual run %s", run_id)
    db.finish_run(run_id, "completed", None)
    return run_id
