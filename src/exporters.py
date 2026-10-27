"""CSV exports with a strict column whitelist and redaction of free-text columns.

Only whitelisted columns are ever written, so response bodies, tokens, server-timing headers and
account identifiers can never leak into an export, even if a caller passes a wider DataFrame.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timezone

import pandas as pd

from src.metrics import METRIC_COLUMNS
from src.redaction import redact_text

EXPORT_RESULT_COLUMNS = [
    "run_id", "endpoint_id", "endpoint_name", "method", "path", "seq", "is_warmup",
    "connection_mode", "started_at", "completed_at", "latency_ms", "http_status", "success", "outcome",
    "is_timeout", "error_type", "error_message", "response_bytes", "api_stat", "source", "is_synthetic",
]
EXPORT_METRIC_COLUMNS = ["run_id", *METRIC_COLUMNS, "computed_at"]
_BOOL_COLUMNS = ("is_warmup", "success", "is_timeout", "is_synthetic", "p95_reliable", "p99_reliable")
_FORMULA_PREFIXES = ("=", "+", "@", "\t", "\r")


def _safe_text(value: object, secrets: list[str]) -> object:
    if value is None or (not isinstance(value, (list, tuple)) and pd.isna(value)):
        return ""
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(v) for v in value)
    text = redact_text(str(value), secrets)
    if text.startswith(_FORMULA_PREFIXES):  # neutralise spreadsheet formula injection
        text = "'" + text
    return text


def _prepare(df: pd.DataFrame | None, columns: list[str], secrets: Iterable[str]) -> pd.DataFrame:
    secret_list = [s for s in secrets if s]
    src = df if df is not None else pd.DataFrame()
    out = pd.DataFrame(index=src.index)
    for col in columns:
        out[col] = src[col] if col in src.columns else None
    for col in columns:
        series = out[col]
        if col in _BOOL_COLUMNS:
            out[col] = series.map(lambda v: "" if v is None or pd.isna(v) else int(bool(v)))
        elif not (pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series)):
            out[col] = series.map(lambda v: _safe_text(v, secret_list)).astype(object)
    return out


def _to_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False, lineterminator="\n").encode("utf-8")


def results_to_csv(df: pd.DataFrame, secrets: Iterable[str] = ()) -> bytes:
    """Request-level results as UTF-8 CSV (whitelisted columns, text columns redacted)."""
    return _to_bytes(_prepare(df, EXPORT_RESULT_COLUMNS, secrets))


def metrics_to_csv(df: pd.DataFrame, secrets: Iterable[str] = ()) -> bytes:
    """Endpoint metrics as UTF-8 CSV; ``warnings`` lists are joined with ``"; "``."""
    return _to_bytes(_prepare(df, EXPORT_METRIC_COLUMNS, secrets))


def export_filename(kind: str, run_ids: list[str] | None = None) -> str:
    """Return e.g. ``tsbench_results_run-20261016-101500-ab12cd_20261016-110000.csv``."""
    safe_kind = re.sub(r"[^a-z0-9_-]+", "_", (kind or "export").lower()).strip("_") or "export"
    if not run_ids:
        scope = "all"
    elif len(run_ids) == 1:
        scope = re.sub(r"[^A-Za-z0-9_-]+", "_", run_ids[0])
    else:
        scope = f"{len(run_ids)}runs"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"tsbench_{safe_kind}_{scope}_{stamp}.csv"
