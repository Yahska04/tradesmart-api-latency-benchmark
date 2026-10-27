"""Interactive single-endpoint check: send a few timed requests to ONE selected endpoint.

Used by the "Check an endpoint" page for quick verification (does it answer, how fast, what does
TradeSmart return?) without configuring a full benchmark. The same safety rules apply as for
benchmarks: only endpoints that pass :func:`safety.assert_safe_to_execute` are ever sent, requests are
paced by the endpoint's rate limiter plus a minimum delay, nothing is retried, and the check stops at
the first rate-limit (HTTP 429) response.

Results are measurements like any other: they can optionally be saved as a regular ``http`` run so
they show up in analytics and history (small samples carry the usual percentile warnings).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

import pandas as pd

from src.metrics import endpoint_metrics
from src.redaction import redact_mapping, redact_text
from src.safety import assert_safe_to_execute, rate_limiter_for

if TYPE_CHECKING:
    from src.api_client import RequestResult, TradeSmartClient
    from src.database import Database
    from src.endpoint_registry import Endpoint

logger = logging.getLogger("tsbench")

MAX_CHECK_REQUESTS = 20
PREVIEW_MAX_CHARS = 6000
PREVIEW_MAX_ITEMS = 5  # list responses (order book, holdings...) are truncated to the first N rows
# In TradeSmart bodies "token" is the instrument token (session tokens use other keys), so previews show it.
PREVIEW_KEEP_KEYS = ("token",)


@dataclass
class CheckOutcome:
    """Result of :func:`run_check`. ``preview`` is a redacted, truncated rendering of the last body."""

    run_id: str
    results: list["RequestResult"] = field(default_factory=list)
    preview: str | None = None
    preview_note: str = ""
    stop_reason: str | None = None

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame([r.to_row() for r in self.results])


def preview_body(body: bytes | None, secrets: list[str] | tuple[str, ...] = ()) -> tuple[str | None, str]:
    """Return ``(text, note)``: a redacted, pretty-printed and truncated view of a response body.

    Sensitive keys (tokens, uid/actid, account numbers ...) are masked via :func:`redact_mapping`; long
    lists are cut to :data:`PREVIEW_MAX_ITEMS` rows. Nothing here is stored or logged.
    """
    if not body:
        return None, "Empty response body."
    try:
        data: Any = json.loads(body)
    except ValueError:
        text = redact_text(body[:PREVIEW_MAX_CHARS].decode("utf-8", "replace"), secrets)
        return text, "Response is not JSON; showing raw text (redacted)."
    notes = ["Sensitive fields are masked."]
    if isinstance(data, list) and len(data) > PREVIEW_MAX_ITEMS:
        notes.append(f"Showing {PREVIEW_MAX_ITEMS} of {len(data)} items.")
        data = data[:PREVIEW_MAX_ITEMS]
    text = json.dumps(redact_mapping(data, secrets, keep_keys=PREVIEW_KEEP_KEYS), indent=2, ensure_ascii=False,
                      default=str)
    if len(text) > PREVIEW_MAX_CHARS:
        text = text[:PREVIEW_MAX_CHARS] + "\n…"
        notes.append(f"Truncated to {PREVIEW_MAX_CHARS} characters.")
    return text, " ".join(notes)


def run_check(
    ep: "Endpoint",
    payload: dict,
    client: "TradeSmartClient",
    *,
    run_id: str,
    requests: int = 3,
    delay_ms: int = 500,
    environment: str = "live",
    secrets: list[str] | tuple[str, ...] = (),
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_result: Callable[["RequestResult"], None] | None = None,
) -> CheckOutcome:
    """Send ``requests`` (1..MAX_CHECK_REQUESTS) timed POSTs to ``ep`` and return the outcome.

    Raises :class:`safety.UnsafeEndpointError` before anything is sent when the endpoint is not allowed
    (e.g. any order endpoint, always, in ``live``). Never retries; stops early on HTTP 429.
    """
    assert_safe_to_execute(ep, environment=environment)
    n = max(1, min(int(requests), MAX_CHECK_REQUESTS))
    limiter = rate_limiter_for(ep, min_interval_s=max(0, delay_ms) / 1000.0, sleep=sleep, clock=clock)
    outcome = CheckOutcome(run_id=run_id)
    for seq in range(1, n + 1):
        limiter.acquire()
        result = client.timed_post(ep, payload, run_id=run_id, seq=seq, is_warmup=False)
        outcome.results.append(result)
        outcome.preview, outcome.preview_note = preview_body(client.last_response_body, secrets)
        if on_result:
            on_result(result)
        if result.outcome == "rate_limited":
            outcome.stop_reason = "Stopped: TradeSmart returned HTTP 429 (rate limit). Wait before retrying."
            break
    logger.info("Endpoint check %s: %d request(s) to %s", run_id, len(outcome.results), ep.id)
    return outcome


def save_check(
    db: "Database",
    ep: "Endpoint",
    outcome: CheckOutcome,
    *,
    base_url: str,
    connection_mode: str,
    timeout_s: float,
    delay_ms: int,
    environment: str,
) -> str:
    """Persist a finished check as a regular ``http`` run (metrics computed from its samples)."""
    config = {
        "endpoint_ids": [ep.id], "iterations": len(outcome.results), "warmup": 0, "timeout_s": timeout_s,
        "delay_ms": delay_ms, "connection_mode": connection_mode, "order": "grouped", "concurrency": 1,
        "environment": environment, "source": "endpoint_check",
    }
    db.create_run(run_id=outcome.run_id, run_type="http", environment=environment,
                  base_host=urlparse(base_url).hostname or "", connection_mode=connection_mode,
                  config=config, planned_requests=len(outcome.results),
                  notes=f"Endpoint check: {ep.name} ({ep.id})")
    db.insert_results(outcome.results)
    db.save_endpoint_metrics(outcome.run_id, endpoint_metrics(outcome.frame()))
    db.finish_run(outcome.run_id, "stopped" if outcome.stop_reason else "completed", outcome.stop_reason)
    return outcome.run_id
