"""Synthetic, clearly labelled demo data for exploring the dashboard without credentials.

Every demo run is stored with ``run_type="demo"``, ``is_synthetic=1``, ``notes=DEMO_LABEL`` and
every request row with ``source="synthetic"``. Latencies are drawn from per-endpoint lognormal
distributions (seeded, so repeatable) with a small long tail, ~1-3 % API/HTTP errors and rare
timeouts. Nothing here touches the network.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from src import metrics as metrics_mod

if TYPE_CHECKING:  # pragma: no cover
    from src.database import Database
    from src.endpoint_registry import Endpoint

DEMO_LABEL = "SYNTHETIC DEMO DATA"
DEMO_BASE_HOST = "synthetic.demo (no network)"
DEMO_TIMEOUT_S = 10.0

# Used only when the caller supplies no read-only endpoints.
DEFAULT_DEMO_ENDPOINTS: list[dict[str, Any]] = [
    {"id": "UserDetails", "name": "User Details", "category": "Authentication", "method": "POST", "path": "/UserDetails"},
    {"id": "Limits", "name": "Limits", "category": "Holdings & Limits", "method": "POST", "path": "/Limits"},
    {"id": "Holdings", "name": "Holdings", "category": "Holdings & Limits", "method": "POST", "path": "/Holdings"},
    {"id": "OrderBook", "name": "Order Book", "category": "Orders & Trades", "method": "POST", "path": "/OrderBook"},
    {"id": "GetQuotes", "name": "Get Quotes", "category": "Market Data", "method": "POST", "path": "/GetQuotes"},
    {"id": "SearchScrip", "name": "Search Scrip", "category": "Scrips / Market Information", "method": "POST", "path": "/SearchScrip"},
]


def _attr(ep: Any, name: str, default: Any = None) -> Any:
    if isinstance(ep, dict):
        return ep.get(name, default)
    return getattr(ep, name, default)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _read_only(endpoints: list[Any]) -> list[dict[str, Any]]:
    selected = []
    for ep in endpoints or []:
        if _attr(ep, "safety_class", "read_only") != "read_only":
            continue
        ep_id = _attr(ep, "id")
        if not ep_id:
            continue
        selected.append(
            {
                "id": ep_id,
                "name": _attr(ep, "name") or ep_id,
                "category": _attr(ep, "category") or "",
                "method": _attr(ep, "method") or "POST",
                "path": _attr(ep, "path") or f"/{ep_id}",
            }
        )
    return selected or [dict(e) for e in DEFAULT_DEMO_ENDPOINTS]


def _sample(rng: np.random.Generator, profile: dict[str, float], cold: bool) -> dict[str, Any]:
    """Draw one synthetic request outcome for an endpoint profile."""
    latency = float(rng.lognormal(np.log(profile["median_ms"]), profile["sigma"]))
    if cold:
        latency += float(rng.uniform(40.0, 90.0))  # extra DNS/TCP/TLS handshake time
    if rng.random() < 0.02:
        latency *= float(rng.uniform(2.5, 6.0))  # long-tail outlier
    roll = rng.random()
    if roll < profile["timeout_p"]:
        return {
            "latency_ms": DEMO_TIMEOUT_S * 1000 + float(rng.uniform(0.5, 5.0)), "http_status": None,
            "success": False, "outcome": "timeout", "is_timeout": True, "error_type": "ReadTimeout",
            "error_message": f"Synthetic timeout after {DEMO_TIMEOUT_S:.1f} s (demo data)",
            "response_bytes": 0, "api_stat": None,
        }
    if roll < profile["timeout_p"] + profile["fail_p"]:
        if rng.random() < 0.6:
            return {
                "latency_ms": latency, "http_status": 200, "success": False, "outcome": "api_error",
                "is_timeout": False, "error_type": "api_error",
                "error_message": "Synthetic API error: Not_Ok (demo data)",
                "response_bytes": int(rng.integers(40, 120)), "api_stat": "Not_Ok",
            }
        status = int(rng.choice([500, 502, 503]))
        return {
            "latency_ms": latency, "http_status": status, "success": False, "outcome": "http_error",
            "is_timeout": False, "error_type": "http_error",
            "error_message": f"Synthetic HTTP {status} (demo data)",
            "response_bytes": int(rng.integers(0, 300)), "api_stat": None,
        }
    return {
        "latency_ms": latency, "http_status": 200, "success": True, "outcome": "success",
        "is_timeout": False, "error_type": None, "error_message": None,
        "response_bytes": int(profile["bytes"] + rng.integers(-50, 50)), "api_stat": "Ok",
    }


def generate_demo_runs(
    db: Database,
    endpoints: list[Endpoint],
    n_runs: int = 3,
    iterations: int = 120,
    seed: int = 42,
    *,
    warmup: int = 2,
    now: datetime | None = None,
) -> list[str]:
    """Create ``n_runs`` synthetic demo runs over the read-only ``endpoints``; return run ids.

    Latencies are deterministic for a given ``seed``; ``now`` anchors run timestamps (runs are
    spread over the preceding days). Endpoint metrics are computed and saved for every run.
    """
    rng = np.random.default_rng(seed)
    eps = _read_only(endpoints)
    profiles = {
        ep["id"]: {
            "median_ms": float(rng.uniform(60.0, 400.0)),
            "sigma": float(rng.uniform(0.18, 0.4)),
            "fail_p": float(rng.uniform(0.01, 0.03)),
            "timeout_p": 0.003,
            "bytes": float(rng.integers(300, 4000)),
        }
        for ep in eps
    }
    anchor = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    delay_s = 0.5
    run_ids: list[str] = []

    for run_index in range(n_runs):
        connection_mode = "warm" if run_index % 2 == 0 else "cold"
        days_back = n_runs - run_index
        start = anchor - timedelta(days=days_back, hours=float(rng.uniform(0, 8)))
        run_id = f"demo-{start:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        drift = float(rng.lognormal(0.0, 0.08))  # run-to-run network variation
        config = {
            "endpoint_ids": [e["id"] for e in eps], "iterations": iterations, "warmup": warmup,
            "timeout_s": DEMO_TIMEOUT_S, "delay_ms": int(delay_s * 1000), "connection_mode": connection_mode,
            "order": "round_robin", "concurrency": 1, "environment": "demo", "synthetic": True,
            "seed": seed, "notes": DEMO_LABEL,
        }
        db.create_run(
            run_id=run_id, run_type="demo", environment="demo", base_host=DEMO_BASE_HOST,
            connection_mode=connection_mode, config=config,
            planned_requests=(iterations + warmup) * len(eps), is_synthetic=True, notes=DEMO_LABEL,
            started_at=_iso(start),
        )

        rows: list[dict[str, Any]] = []
        cursor = start
        seq_by_ep = {e["id"]: 0 for e in eps}
        for round_index in range(warmup + iterations):
            is_warmup = round_index < warmup
            for ep in eps:
                profile = dict(profiles[ep["id"]])
                profile["median_ms"] *= drift * (1.4 if is_warmup else 1.0)
                outcome = _sample(rng, profile, cold=connection_mode == "cold" or is_warmup)
                seq_by_ep[ep["id"]] += 1
                started = cursor
                completed = started + timedelta(milliseconds=outcome["latency_ms"])
                rows.append(
                    {
                        "run_id": run_id, "endpoint_id": ep["id"], "endpoint_name": ep["name"],
                        "path": ep["path"], "method": ep["method"], "seq": seq_by_ep[ep["id"]],
                        "is_warmup": is_warmup, "connection_mode": connection_mode,
                        "started_at": _iso(started), "completed_at": _iso(completed),
                        "server_timing": None, "source": "synthetic", "is_synthetic": True,
                        "category": ep["category"],
                        **outcome,
                    }
                )
                cursor = completed + timedelta(seconds=delay_s)

        db.insert_results([{k: v for k, v in r.items() if k != "category"} for r in rows])
        db.save_endpoint_metrics(run_id, metrics_mod.endpoint_metrics(pd.DataFrame(rows)))
        db.finish_run(run_id, "completed", stop_reason=None, completed_at=_iso(cursor))
        run_ids.append(run_id)
    return run_ids


def clear_demo_runs(db: Database) -> int:
    """Delete all synthetic runs (and, by cascade, their rows); real runs are untouched."""
    return db.delete_synthetic_runs()
