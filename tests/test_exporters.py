"""Tests for src.exporters: whitelist, redaction, filenames."""
from __future__ import annotations

import io

import pandas as pd

from src.exporters import EXPORT_RESULT_COLUMNS, export_filename, metrics_to_csv, results_to_csv
from src.metrics import METRIC_COLUMNS


def _results_df() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "id": 1, "run_id": "run-1", "endpoint_id": "UserDetails", "endpoint_name": "User Details",
            "method": "POST", "path": "/UserDetails", "seq": 1, "is_warmup": False, "connection_mode": "warm",
            "started_at": "2026-10-16T10:00:00.000000Z", "completed_at": "2026-10-16T10:00:00.100000Z",
            "latency_ms": 100.5, "http_status": 500, "success": False, "outcome": "http_error",
            "is_timeout": False, "error_type": "http_error",
            "error_message": 'Bearer tok.en.value jData={"uid":"FA12345","actid":"FA12345"} acct 9876543210',
            "response_bytes": 10, "api_stat": None, "server_timing": "app;dur=12", "source": "measured",
            "is_synthetic": False,
            # columns that must never be exported
            "response_body": '{"susertoken":"LEAKED"}', "access_token": "LEAKED", "client_id": "FA12345",
        }
    ])


def test_results_csv_whitelist_and_redaction():
    raw = results_to_csv(_results_df(), secrets=["tok.en.value"])
    text = raw.decode("utf-8")
    out = pd.read_csv(io.StringIO(text))
    assert list(out.columns) == EXPORT_RESULT_COLUMNS
    for forbidden in ("LEAKED", "FA12345", "tok.en.value", "9876543210", "response_body", "server_timing",
                      "access_token"):
        assert forbidden not in text
    assert "***REDACTED***" in text
    assert out.loc[0, "latency_ms"] == 100.5
    assert out.loc[0, "success"] == 0


def test_results_csv_empty_and_missing_columns():
    text = results_to_csv(pd.DataFrame()).decode("utf-8")
    assert text.strip() == ",".join(EXPORT_RESULT_COLUMNS)
    text = results_to_csv(pd.DataFrame([{"run_id": "r", "latency_ms": 1.0}])).decode("utf-8")
    assert text.splitlines()[0] == ",".join(EXPORT_RESULT_COLUMNS)


def test_formula_injection_neutralised():
    df = _results_df()
    df.loc[0, "error_message"] = "=HYPERLINK(\"http://x\")"
    out = pd.read_csv(io.StringIO(results_to_csv(df).decode("utf-8")))
    assert out.loc[0, "error_message"].startswith("'=")


def test_metrics_csv():
    df = pd.DataFrame([{c: None for c in METRIC_COLUMNS} | {
        "run_id": "run-1", "endpoint_id": "Limits", "samples": 10, "p50_ms": 50.0,
        "warnings": ["P99 based on 10 samples (< 100); treat as indicative only", "uid=FA1"],
        "p95_reliable": False, "secret_key": "LEAKED"}])
    text = metrics_to_csv(df).decode("utf-8")
    out = pd.read_csv(io.StringIO(text))
    assert "secret_key" not in out.columns and "LEAKED" not in text and "FA1" not in text
    assert out.loc[0, "endpoint_id"] == "Limits"
    assert "P99 based on 10 samples" in out.loc[0, "warnings"]
    assert out.loc[0, "p95_reliable"] == 0


def test_export_filename():
    assert export_filename("results", ["run-20261016-101500-abc123"]).startswith(
        "tsbench_results_run-20261016-101500-abc123_")
    assert export_filename("metrics", ["a", "b"]).startswith("tsbench_metrics_2runs_")
    name = export_filename("../evil kind", None)
    assert name.endswith(".csv") and "/" not in name and "\\" not in name and "_all_" in name
