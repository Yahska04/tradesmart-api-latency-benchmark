"""Latency statistics, per-endpoint metrics and run comparison.

Method (stated explicitly so results are reproducible):

* Percentiles use ``numpy.percentile(values, q, method="linear")`` (NumPy's default, type 7).
* Percentiles, min, max, mean and standard deviation (``ddof=1``) are computed ONLY from
  successful, non-warmup requests that have a finite latency; ``samples`` is that count.
* Failures, timeouts and rate-limited requests are counted separately and never contribute
  latency samples, so a fast failure cannot make an endpoint look faster.
* P95 is flagged reliable only with >= 20 samples, P99 only with >= 100 samples.
* Empty inputs never raise: statistics are ``None``.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

PERCENTILE_METHOD = "linear"
MIN_SAMPLES_P95 = 20
MIN_SAMPLES_P99 = 100
METRIC_COLUMNS = [
    "endpoint_id", "endpoint_name", "method", "category", "samples", "total", "successful",
    "failed", "timeouts", "rate_limited", "success_rate", "failure_rate", "p50_ms", "p95_ms", "p99_ms",
    "min_ms", "max_ms", "mean_ms", "std_ms", "rps", "p95_reliable", "p99_reliable", "warnings",
]
COMPARE_METRICS = ("p50", "p95", "p99", "success_rate", "failure_rate")

_SUMMARY_KEYS = [c for c in METRIC_COLUMNS if c not in ("endpoint_id", "endpoint_name", "method", "category")]


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _clean(values: Sequence[float] | pd.Series | np.ndarray) -> np.ndarray:
    arr = pd.to_numeric(pd.Series(list(values), dtype="object"), errors="coerce").to_numpy(dtype=float)
    return arr[np.isfinite(arr)]


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile of the finite values, or ``None`` when there are none."""
    arr = _clean(values)
    if arr.size == 0:
        return None
    return float(np.percentile(arr, q, method=PERCENTILE_METHOD))


def _bool_col(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(False, index=df.index, dtype=bool)
    s = df[col]

    def conv(v: Any) -> bool:
        if _is_missing(v):
            return False
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "y", "t")
        return bool(v)

    return s.map(conv).astype(bool)


def _wall_seconds(df: pd.DataFrame) -> float | None:
    if len(df) < 2 or "started_at" not in df.columns or "completed_at" not in df.columns:
        return None
    start = pd.to_datetime(df["started_at"], utc=True, errors="coerce", format="ISO8601").min()
    end = pd.to_datetime(df["completed_at"], utc=True, errors="coerce", format="ISO8601").max()
    if pd.isna(start) or pd.isna(end):
        return None
    seconds = (end - start).total_seconds()
    return seconds if seconds > 0 else None


def summarize(df: pd.DataFrame) -> dict:
    """Summarise request-level rows (``RequestResult.to_row`` columns); warmup rows are excluded.

    Returns a dict with every non-identity key of :data:`METRIC_COLUMNS`.
    """
    if df is None:
        df = pd.DataFrame()
    measured = df[~_bool_col(df, "is_warmup")] if len(df) else df
    total = int(len(measured))
    success_mask = _bool_col(measured, "success") if total else pd.Series(dtype=bool)
    successful = int(success_mask.sum()) if total else 0
    failed = total - successful
    timeouts = int(_bool_col(measured, "is_timeout").sum()) if total else 0
    rate_limited = (
        int((measured["outcome"] == "rate_limited").sum()) if total and "outcome" in measured.columns else 0
    )

    if total and "latency_ms" in measured.columns:
        lat = _clean(measured.loc[success_mask, "latency_ms"])
    else:
        lat = np.array([], dtype=float)
    samples = int(lat.size)

    warnings: list[str] = []
    if total == 0:
        warnings.append("No measured (non-warmup) requests; no statistics available")
    elif successful == 0:
        warnings.append("No successful requests; latency percentiles unavailable")
    elif samples < successful:
        warnings.append(f"{successful - samples} successful request(s) had no recorded latency and were skipped")
    if 0 < samples < MIN_SAMPLES_P95:
        warnings.append(
            f"P95 based on {samples} samples (< {MIN_SAMPLES_P95}); treat as indicative only"
        )
    if 0 < samples < MIN_SAMPLES_P99:
        warnings.append(
            f"P99 based on {samples} samples (< {MIN_SAMPLES_P99}); treat as indicative only"
        )
    if failed:
        warnings.append(
            f"{failed} failed request(s) ({timeouts} timeout(s)) excluded from latency percentiles"
        )

    wall = _wall_seconds(measured)
    return {
        "samples": samples,
        "total": total,
        "successful": successful,
        "failed": failed,
        "timeouts": timeouts,
        "rate_limited": rate_limited,
        "success_rate": successful / total if total else None,
        "failure_rate": failed / total if total else None,
        "p50_ms": percentile(lat, 50),
        "p95_ms": percentile(lat, 95),
        "p99_ms": percentile(lat, 99),
        "min_ms": float(lat.min()) if samples else None,
        "max_ms": float(lat.max()) if samples else None,
        "mean_ms": float(lat.mean()) if samples else None,
        "std_ms": float(np.std(lat, ddof=1)) if samples >= 2 else None,
        "rps": total / wall if wall else None,
        "p95_reliable": samples >= MIN_SAMPLES_P95,
        "p99_reliable": samples >= MIN_SAMPLES_P99,
        "warnings": warnings,
    }


def _first_valid(series: pd.Series) -> Any:
    valid = series.dropna()
    return valid.iloc[0] if len(valid) else None


def endpoint_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """One row per ``endpoint_id`` with :data:`METRIC_COLUMNS` (warmup rows excluded)."""
    if df is None or len(df) == 0 or "endpoint_id" not in df.columns:
        return pd.DataFrame(columns=METRIC_COLUMNS)
    measured = df[~_bool_col(df, "is_warmup")]
    rows: list[dict] = []
    for endpoint_id, group in measured.groupby("endpoint_id", sort=False, dropna=False):
        row: dict[str, Any] = {
            "endpoint_id": endpoint_id,
            "endpoint_name": _first_valid(group["endpoint_name"]) if "endpoint_name" in group else None,
            "method": _first_valid(group["method"]) if "method" in group else None,
            "category": _first_valid(group["category"]) if "category" in group else None,
        }
        row.update(summarize(group))
        rows.append(row)
    return pd.DataFrame(rows, columns=METRIC_COLUMNS)


def overall_metrics(df: pd.DataFrame) -> dict:
    """Summary over all non-warmup rows plus ``endpoints`` (distinct endpoint count)."""
    result = summarize(df)
    if df is not None and len(df) and "endpoint_id" in df.columns:
        measured = df[~_bool_col(df, "is_warmup")]
        result["endpoints"] = int(measured["endpoint_id"].nunique())
    else:
        result["endpoints"] = 0
    return result


def safe_change(baseline: float | None, candidate: float | None) -> tuple[float | None, float | None]:
    """Return ``(candidate - baseline, percent change)``.

    ``abs`` is ``None`` if either value is missing; ``pct`` is ``None`` when the baseline is
    missing, zero or NaN (percent change is undefined).
    """
    base = None if _is_missing(baseline) else float(baseline)  # type: ignore[arg-type]
    cand = None if _is_missing(candidate) else float(candidate)  # type: ignore[arg-type]
    if base is None or cand is None:
        return None, None
    diff = cand - base
    if base == 0 or not math.isfinite(base):
        return diff, None
    return diff, diff / abs(base) * 100.0


def compare_runs(baseline: pd.DataFrame, candidate: pd.DataFrame) -> pd.DataFrame:
    """Compare two :func:`endpoint_metrics` frames (outer join on ``endpoint_id``)."""
    columns = ["endpoint_id", "endpoint_name"]
    for m in COMPARE_METRICS:
        columns += [f"{m}_base", f"{m}_cand", f"{m}_abs", f"{m}_pct"]
    columns.append("status")

    def index(frame: pd.DataFrame | None) -> dict[str, dict]:
        if frame is None or len(frame) == 0 or "endpoint_id" not in frame.columns:
            return {}
        return {str(r["endpoint_id"]): r for r in frame.to_dict("records")}

    base_rows, cand_rows = index(baseline), index(candidate)
    ordered = list(base_rows) + [k for k in cand_rows if k not in base_rows]
    out: list[dict] = []
    for ep_id in ordered:
        b, c = base_rows.get(ep_id), cand_rows.get(ep_id)
        name = (b or {}).get("endpoint_name")
        if _is_missing(name):
            name = (c or {}).get("endpoint_name")
        row: dict[str, Any] = {"endpoint_id": ep_id, "endpoint_name": None if _is_missing(name) else name}
        for m in COMPARE_METRICS:
            key = f"{m}_ms" if m.startswith("p") and m[1:].isdigit() else m
            bv = None if b is None or _is_missing(b.get(key)) else float(b[key])
            cv = None if c is None or _is_missing(c.get(key)) else float(c[key])
            diff, pct = safe_change(bv, cv)
            row.update({f"{m}_base": bv, f"{m}_cand": cv, f"{m}_abs": diff, f"{m}_pct": pct})
        row["status"] = "both" if b is not None and c is not None else ("baseline_only" if b is not None else "candidate_only")
        out.append(row)
    return pd.DataFrame(out, columns=columns)


_CONDITION_FIELDS: list[tuple[str, str]] = [
    ("run_type", "Run type"),
    ("connection_mode", "Connection mode"),
    ("environment", "Environment"),
    ("base_host", "Base host"),
    ("iterations", "Iterations"),
    ("warmup", "Warmup requests"),
    ("delay_ms", "Delay between requests (ms)"),
    ("timeout_s", "Timeout (s)"),
    ("concurrency", "Concurrency"),
    ("order", "Request order"),
]


def comparable_conditions(run_a: dict, run_b: dict) -> list[str]:
    """Human-readable list of test-condition differences between two runs (``Database.get_run``).

    An empty list means the runs were measured under the same recorded conditions.
    """
    if not run_a or not run_b:
        return ["Run metadata missing for one of the runs; conditions cannot be compared"]

    def value(run: dict, key: str) -> Any:
        if key in run and not _is_missing(run.get(key)):
            return run.get(key)
        config = run.get("config") or {}
        v = config.get(key) if isinstance(config, dict) else None
        return None if _is_missing(v) else v

    def norm(v: Any) -> Any:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        return v

    diffs: list[str] = []
    for key, label in _CONDITION_FIELDS:
        a, b = value(run_a, key), value(run_b, key)
        if a is None and b is None:
            continue
        if norm(a) != norm(b):
            diffs.append(f"{label} differs: {a if a is not None else 'n/a'} vs {b if b is not None else 'n/a'}")
    if bool(run_a.get("is_synthetic")) != bool(run_b.get("is_synthetic")):
        diffs.append("One run is SYNTHETIC demo data and the other is measured data")
    return diffs
