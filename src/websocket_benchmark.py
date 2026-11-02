"""WebSocket market-data benchmark (kept separate from HTTP endpoint latency).

Protocol (docs/TradeSmart_API_V2.md sections 28-34): after opening the socket send
``{"t":"a","uid","actid","source":"API","accesstoken"}`` and wait for ``t=ak``; subscribe touchline
(``t=t`` -> ack ``tk``, updates ``tf``) or depth (``t=d`` -> ack ``dk``, updates ``df``) with
``k="NSE|22#BSE|508123"``; send ``{"t":"h"}`` heartbeats.

What is measured (client-observed, via ``time.perf_counter_ns``): connection establishment, auth ack,
subscription ack, time to first feed message, and the *intervals between message arrivals*. These are
NOT exchange-to-client latency: feed messages carry no trustworthy send timestamp, and arrival intervals
mostly reflect how often the market (and server) produce updates.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

import numpy as np

from src.redaction import mask_secret, redact_text

if TYPE_CHECKING:
    from src.database import Database

logger = logging.getLogger("tsbench")

MAX_DURATION_S = 300.0
FEED_UPDATE_TYPES = {"t": "tf", "d": "df"}
FEED_ACK_TYPES = {"t": "tk", "d": "dk"}
UNSUBSCRIBE_TYPES = {"t": "u", "d": "ud"}
METRIC_LABEL = "Client-observed message-arrival intervals (NOT exchange-to-client latency)"
_POLL_S = 0.25  # max time blocked in recv before re-checking stop/heartbeat/deadline


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class WSBenchmarkConfig:
    """WebSocket benchmark settings. ``duration_s`` is capped at 300 s."""

    ws_url: str
    client_id: str
    access_token: str
    instruments: list[str]
    feed: str = "t"
    duration_s: float = 30.0
    heartbeat_s: float = 30.0
    max_reconnects: int = 2
    connect_timeout_s: float = 10.0

    def validate(self) -> list[str]:
        """Return configuration problems (``[]`` = valid)."""
        p: list[str] = []
        if not self.ws_url.startswith(("ws://", "wss://")):
            p.append("ws_url must start with ws:// or wss://")
        if not self.client_id or not self.access_token:
            p.append("client id and access token are required")
        if not self.instruments:
            p.append("at least one instrument (EXCH|token) is required")
        bad = [i for i in self.instruments if "|" not in i]
        if bad:
            p.append(f"instruments must look like 'NSE|22': {', '.join(bad)}")
        if self.feed not in FEED_UPDATE_TYPES:
            p.append("feed must be 't' (touchline) or 'd' (depth)")
        if self.duration_s <= 0:
            p.append("duration_s must be positive")
        if self.heartbeat_s <= 0:
            p.append("heartbeat_s must be positive")
        if self.max_reconnects < 0:
            p.append("max_reconnects must be >= 0")
        return p

    def to_dict(self) -> dict:
        """Serialisable settings without secrets (token removed, client id masked)."""
        d = asdict(self)
        d["access_token"] = "***" if self.access_token else ""
        d["client_id"] = mask_secret(self.client_id)
        d["duration_s"] = min(float(self.duration_s), MAX_DURATION_S)
        return d


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {k: None for k in ("p50", "p95", "p99", "min", "max", "mean")}
    arr = np.asarray(values, dtype=float)
    return {
        "p50": float(np.percentile(arr, 50, method="linear")),
        "p95": float(np.percentile(arr, 95, method="linear")),
        "p99": float(np.percentile(arr, 99, method="linear")),
        "min": float(arr.min()), "max": float(arr.max()), "mean": float(arr.mean()),
    }


@dataclass
class WSBenchmarkResult:
    """Raw WebSocket observations. Use :meth:`summary` for display."""

    run_id: str
    connect_ms: float | None
    auth_ack_ms: float | None
    subscribe_ack_ms: float | None
    first_message_ms: float | None
    inter_arrival_ms: list[float]
    message_counts: dict[str, int]
    disconnects: int
    reconnects: int
    errors: list[str]
    started_at: str
    completed_at: str
    connect_samples_ms: list[float] = field(default_factory=list)  # every successful (re)connect
    duration_s: float = 0.0

    def summary(self) -> dict:
        """Display summary. Interval statistics are message-arrival intervals, NOT exchange latency."""
        stats = _stats(self.inter_arrival_ms)
        n = len(self.inter_arrival_ms)
        total_messages = int(sum(self.message_counts.values()))
        warnings: list[str] = []
        if n == 0:
            warnings.append("No feed updates received (market closed, invalid instrument, or auth failure?).")
        elif n < 20:
            warnings.append(f"Only {n} intervals; P95 is indicative only.")
        elif n < 100:
            warnings.append(f"P99 based on {n} intervals (< 100); treat as indicative only.")
        long_gaps = 0
        if stats["p50"] is not None:
            threshold = max(1000.0, 10 * stats["p50"])
            long_gaps = sum(1 for v in self.inter_arrival_ms if v > threshold)
        return {
            "run_id": self.run_id,
            "label": METRIC_LABEL,
            "note": ("Arrival intervals reflect how often updates are published and delivered; they are not "
                     "one-way network latency or exchange event latency."),
            "connect_ms": self.connect_ms,
            "auth_ack_ms": self.auth_ack_ms,
            "subscribe_ack_ms": self.subscribe_ack_ms,
            "first_message_ms": self.first_message_ms,
            "interval_count": n,
            "interval_p50_ms": stats["p50"], "interval_p95_ms": stats["p95"], "interval_p99_ms": stats["p99"],
            "interval_min_ms": stats["min"], "interval_max_ms": stats["max"], "interval_mean_ms": stats["mean"],
            "long_pauses": long_gaps,
            "message_counts": dict(self.message_counts),
            "total_messages": total_messages,
            "messages_per_s": (total_messages / self.duration_s) if self.duration_s > 0 else None,
            "disconnects": self.disconnects,
            "reconnects": self.reconnects,
            "error_count": len(self.errors),
            "errors": list(self.errors),
            "duration_s": self.duration_s,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "warnings": warnings,
        }


def _default_connect(cfg: WSBenchmarkConfig) -> Callable[[str], Any]:
    import websockets

    from src import outbound

    def factory(url: str) -> Any:
        # Application-level heartbeat {"t":"h"} is used instead of protocol pings.
        return websockets.connect(url, open_timeout=cfg.connect_timeout_s, ping_interval=None,
                                  close_timeout=2, max_size=2**22, **outbound.websocket_kwargs())

    return factory


def _is_set(stop_event: Any) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


class _AuthRejected(Exception):
    pass


async def run_ws_benchmark(
    cfg: WSBenchmarkConfig,
    connect: Callable | None = None,
    stop_event: Any = None,
    *,
    run_id: str | None = None,
    backoff_base_s: float = 1.0,
) -> WSBenchmarkResult:
    """Connect, authenticate, subscribe and observe the feed for ``cfg.duration_s`` seconds.

    ``connect(url)`` must return an async context manager yielding an object with async ``send``,
    ``recv`` and ``close`` (default: ``websockets.connect``). ``stop_event`` is any object with
    ``is_set()`` (threading/asyncio Event). Reconnects with exponential backoff up to ``max_reconnects``.
    Never raises for connection/protocol errors; they are recorded in ``errors``.
    """
    connect = connect or _default_connect(cfg)
    secrets = [s for s in (cfg.access_token, cfg.client_id) if s]
    duration = min(max(float(cfg.duration_s), 0.0), MAX_DURATION_S)
    run_id = run_id or f"ws-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    instruments_key = "#".join(cfg.instruments)
    update_type = FEED_UPDATE_TYPES.get(cfg.feed, "tf")
    ack_type = FEED_ACK_TYPES.get(cfg.feed, "tk")

    res = WSBenchmarkResult(run_id=run_id, connect_ms=None, auth_ack_ms=None, subscribe_ack_ms=None,
                            first_message_ms=None, inter_arrival_ms=[], message_counts={}, disconnects=0,
                            reconnects=0, errors=[], started_at=_utc_now_iso(), completed_at="")
    counts: Counter[str] = Counter()
    start_ns = time.perf_counter_ns()
    deadline_ns = start_ns + int(duration * 1e9)

    def remaining_s() -> float:
        return (deadline_ns - time.perf_counter_ns()) / 1e9

    def err(msg: str) -> None:
        res.errors.append(redact_text(msg, secrets))

    attempt = 0
    while True:
        if _is_set(stop_event) or remaining_s() <= 0:
            break
        if attempt > 0:
            backoff = min(backoff_base_s * (2 ** (attempt - 1)), 30.0, max(remaining_s(), 0.0))
            if backoff > 0:
                await asyncio.sleep(backoff)
            if _is_set(stop_event) or remaining_s() <= 0:
                break
            res.reconnects += 1
        attempt += 1
        disconnected = False
        connected = False
        try:
            async with contextlib.AsyncExitStack() as stack:
                t0 = time.perf_counter_ns()
                ws = await asyncio.wait_for(stack.enter_async_context(connect(cfg.ws_url)), cfg.connect_timeout_s)
                connect_ms = (time.perf_counter_ns() - t0) / 1e6
                connected = True
                res.connect_samples_ms.append(connect_ms)
                if res.connect_ms is None:
                    res.connect_ms = connect_ms

                auth = {"t": "a", "uid": cfg.client_id, "actid": cfg.client_id, "source": "API",
                        "accesstoken": cfg.access_token}
                t_auth = time.perf_counter_ns()
                await ws.send(json.dumps(auth))
                authed = False
                t_sub: int | None = None
                subscribe_acked = False
                last_feed_ns: int | None = None
                next_hb_ns = time.perf_counter_ns() + int(cfg.heartbeat_s * 1e9)
                auth_deadline_ns = t_auth + int(cfg.connect_timeout_s * 1e9)

                while not _is_set(stop_event) and remaining_s() > 0:
                    now = time.perf_counter_ns()
                    if not authed and now >= auth_deadline_ns:
                        raise TimeoutError("no auth acknowledgement (t=ak) within connect timeout")
                    if now >= next_hb_ns:
                        await ws.send(json.dumps({"t": "h"}))
                        counts["h_sent"] += 1
                        next_hb_ns = now + int(cfg.heartbeat_s * 1e9)
                    wait = min(_POLL_S, remaining_s(), max((next_hb_ns - now) / 1e9, 0.0))
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(wait, 0.001))
                    except asyncio.TimeoutError:
                        continue
                    arrival = time.perf_counter_ns()
                    try:
                        msg = json.loads(raw)
                        mtype = str(msg.get("t", "unknown")) if isinstance(msg, dict) else "non_object"
                    except (ValueError, TypeError):
                        msg, mtype = None, "invalid_json"
                    counts[mtype] += 1

                    if mtype == "ak" and not authed:
                        status = str(msg.get("s", "OK")) if isinstance(msg, dict) else "OK"
                        if status.upper() not in ("OK", ""):
                            raise _AuthRejected(f"authentication rejected (t=ak, s={status})")
                        authed = True
                        if res.auth_ack_ms is None:
                            res.auth_ack_ms = (arrival - t_auth) / 1e6
                        await ws.send(json.dumps({"t": cfg.feed, "k": instruments_key}))
                        t_sub = time.perf_counter_ns()
                    elif mtype == ack_type and t_sub is not None and not subscribe_acked:
                        subscribe_acked = True
                        if res.subscribe_ack_ms is None:
                            res.subscribe_ack_ms = (arrival - t_sub) / 1e6
                    elif mtype == update_type:
                        if res.first_message_ms is None and t_sub is not None:
                            res.first_message_ms = (arrival - t_sub) / 1e6
                        if last_feed_ns is not None:
                            res.inter_arrival_ms.append((arrival - last_feed_ns) / 1e6)
                        last_feed_ns = arrival

                if authed:  # polite unsubscribe; failures are irrelevant at shutdown
                    with contextlib.suppress(Exception):
                        await ws.send(json.dumps({"t": UNSUBSCRIBE_TYPES.get(cfg.feed, "u"), "k": instruments_key}))
                with contextlib.suppress(Exception):
                    await ws.close()
            break  # normal end (duration reached or stop requested)
        except _AuthRejected as exc:
            err(str(exc))
            break  # credentials problem: reconnecting would not help
        except (asyncio.TimeoutError, TimeoutError) as exc:
            err(f"timeout: {exc or 'connect timed out'}")
            disconnected = True
        except Exception as exc:  # noqa: BLE001 - connection closed / network / protocol errors
            err(f"{type(exc).__name__}: {exc}")
            disconnected = True
        if disconnected:
            if connected:
                res.disconnects += 1
            if attempt > cfg.max_reconnects:
                err(f"giving up after {cfg.max_reconnects} reconnect attempt(s)")
                break

    res.message_counts = dict(counts)
    res.completed_at = _utc_now_iso()
    res.duration_s = (time.perf_counter_ns() - start_ns) / 1e9
    return res


def _ws_samples(res: WSBenchmarkResult) -> list[tuple[str, float, str, str | None]]:
    now = res.completed_at or _utc_now_iso()
    samples: list[tuple[str, float, str, str | None]] = []
    for metric in ("connect_ms", "auth_ack_ms", "subscribe_ack_ms", "first_message_ms"):
        value = getattr(res, metric)
        if value is not None:
            samples.append((metric, float(value), now, None))
    samples.extend(("inter_arrival_ms", float(v), now, None) for v in res.inter_arrival_ms)
    if res.disconnects:
        samples.append(("disconnect", float(res.disconnects), now, None))
    if res.reconnects:
        samples.append(("reconnect", float(res.reconnects), now, None))
    for mtype, count in sorted(res.message_counts.items()):
        samples.append(("message_count", float(count), now, mtype))
    return samples


def run_ws_benchmark_sync(
    cfg: WSBenchmarkConfig,
    db: "Database | None" = None,
    *,
    environment: str = "live",
    loop: asyncio.AbstractEventLoop | None = None,
    **kw: Any,
) -> WSBenchmarkResult:
    """Run :func:`run_ws_benchmark` to completion (``asyncio.run`` or the given ``loop``) and persist it.

    With ``db``: ``create_run(run_type="websocket")``, ``insert_ws_samples`` and ``finish_run``.
    ``kw`` is forwarded (``connect``, ``stop_event``, ``backoff_base_s``).
    """
    problems = cfg.validate()
    if problems:
        raise ValueError("invalid WebSocket config: " + "; ".join(problems))
    if "run_id" not in kw or not kw["run_id"]:
        try:
            from src.database import new_run_id

            kw["run_id"] = new_run_id("ws")
        except ImportError:  # pragma: no cover
            kw["run_id"] = None
    run_id = kw["run_id"]
    if db is not None:
        db.create_run(run_id=run_id, run_type="websocket", environment=environment,
                      base_host=urlparse(cfg.ws_url).hostname or "", connection_mode="websocket",
                      config=cfg.to_dict(), planned_requests=0, notes=METRIC_LABEL)
    coro = run_ws_benchmark(cfg, **kw)
    try:
        res = loop.run_until_complete(coro) if loop is not None else asyncio.run(coro)
    except Exception as exc:  # noqa: BLE001
        if db is not None:
            db.finish_run(run_id, "failed", redact_text(f"{type(exc).__name__}: {exc}", [cfg.access_token]))
        raise
    if db is not None:
        db.insert_ws_samples(res.run_id, _ws_samples(res))
        stopped = kw.get("stop_event") is not None and kw["stop_event"].is_set()
        status = "failed" if res.connect_ms is None else ("stopped" if stopped else "completed")
        reason = "; ".join(res.errors)[:1000] if res.errors else None
        db.finish_run(res.run_id, status, reason)
    return res
