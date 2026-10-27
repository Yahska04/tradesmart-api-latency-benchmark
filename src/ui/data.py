"""Data helpers shared by pages (no rendering here)."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

import pandas as pd

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
SECRET_PLACEHOLDERS = {"ClientId"}           # account identifier: masked input
NEVER_PROMPT = {"code", "checksum"}         # auth-exchange values; never collected here


def discover_placeholders(endpoints: Iterable[Any]) -> dict[str, dict]:
    """Placeholders used by the endpoints' body templates -> {example, description, endpoints}."""
    found: dict[str, dict] = {}
    for ep in endpoints:
        template = ep.request_body_template or {}
        params = {p.name: p for p in (ep.params or [])}
        for key, value in _walk(template):
            for name in _PLACEHOLDER.findall(json.dumps(value)):
                info = found.setdefault(name, {"example": None, "description": "", "endpoints": []})
                if ep.id not in info["endpoints"]:
                    info["endpoints"].append(ep.id)
                p = params.get(key) or params.get(name)
                if p is not None:
                    ex = "" if p.example is None else str(p.example)
                    if not info["example"] and ex and not _PLACEHOLDER.search(ex):
                        info["example"] = ex
                    if not info["description"] and p.description:
                        info["description"] = p.description
    return dict(sorted(found.items(), key=lambda kv: (kv[0] != "ClientId", kv[0].lower())))


def _walk(obj: Any, key: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, str(k))
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, key)
    else:
        yield key, obj


def run_endpoint_metrics(db: Any, run_ids: list[str]) -> pd.DataFrame:
    """Per (run, endpoint) metrics: stored metrics when present, else recomputed from rows."""
    from src.metrics import endpoint_metrics

    if not run_ids:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    stored = pd.DataFrame()
    try:
        stored = db.get_metrics(run_ids)
    except Exception:
        stored = pd.DataFrame()
    have = set(stored["run_id"]) if len(stored) and "run_id" in stored.columns else set()
    if have:
        frames.append(stored[stored["run_id"].isin(run_ids)])
    missing = [r for r in run_ids if r not in have]
    if missing:
        rows = db.get_results(run_ids=missing, include_synthetic=True)
        if len(rows):
            for rid, g in rows.groupby("run_id"):
                m = endpoint_metrics(g)
                m.insert(0, "run_id", rid)
                frames.append(m)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    for col in ("p50_ms", "p95_ms", "p99_ms", "success_rate", "failure_rate", "samples"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def runs_frame(db: Any, include_synthetic: bool, run_types: list[str]) -> pd.DataFrame:
    try:
        runs = db.list_runs(include_synthetic=include_synthetic, run_types=run_types)
    except Exception:
        return pd.DataFrame()
    if runs is None:
        return pd.DataFrame()
    if len(runs):
        runs = runs.copy()
        runs["run_time"] = pd.to_datetime(runs["started_at"].fillna(runs["created_at"]),
                                          utc=True, errors="coerce")
    return runs
