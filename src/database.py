"""SQLite persistence for endpoints, benchmark runs, request results, metrics and settings.

A fresh ``sqlite3`` connection is opened per operation (safe with Streamlit's threads). The
database uses WAL journaling and enforces foreign keys, so deleting a run cascades to its
results, metrics and WebSocket samples. No secrets are ever stored here.
"""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from src import metrics as metrics_mod
from src.redaction import redact_text, registered_secrets
from src.settings import APP_VERSION, load_settings

if TYPE_CHECKING:  # pragma: no cover - imports for type checkers only
    from src.api_client import RequestResult
    from src.endpoint_registry import Endpoint
    from src.settings import Settings

RUN_TYPES = ("http", "manual", "demo", "websocket")
RUN_STATUSES = ("running", "completed", "stopped", "failed")

RESULT_COLUMNS = [
    "run_id", "endpoint_id", "endpoint_name", "path", "method", "seq", "is_warmup", "connection_mode",
    "started_at", "completed_at", "latency_ms", "http_status", "success", "outcome", "is_timeout",
    "error_type", "error_message", "response_bytes", "api_stat", "server_timing", "source", "is_synthetic",
]
_RESULT_BOOL_COLUMNS = ("is_warmup", "success", "is_timeout", "is_synthetic")
_METRIC_STORE_COLUMNS = [c for c in metrics_mod.METRIC_COLUMNS if c != "warnings"]
_SECRET_KEY_HINTS = ("token", "secret", "key", "password", "checksum", "client_id")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoints (
    id TEXT PRIMARY KEY,
    name TEXT,
    category TEXT,
    method TEXT,
    path TEXT,
    safety_class TEXT,
    definition_json TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS benchmark_runs (
    run_id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL CHECK (run_type IN ('http','manual','demo','websocket')),
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running','completed','stopped','failed')),
    is_synthetic INTEGER NOT NULL DEFAULT 0,
    environment TEXT,
    base_host TEXT,
    connection_mode TEXT,
    config_json TEXT,
    notes TEXT,
    planned_requests INTEGER,
    stop_reason TEXT,
    app_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON benchmark_runs(started_at);
CREATE TABLE IF NOT EXISTS request_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES benchmark_runs(run_id) ON DELETE CASCADE,
    endpoint_id TEXT,
    endpoint_name TEXT,
    path TEXT,
    method TEXT,
    seq INTEGER,
    is_warmup INTEGER NOT NULL DEFAULT 0,
    connection_mode TEXT,
    started_at TEXT,
    completed_at TEXT,
    latency_ms REAL,
    http_status INTEGER,
    success INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    is_timeout INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    error_message TEXT,
    response_bytes INTEGER,
    api_stat TEXT,
    server_timing TEXT,
    source TEXT,
    is_synthetic INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_results_run ON request_results(run_id);
CREATE INDEX IF NOT EXISTS idx_results_endpoint ON request_results(endpoint_id);
CREATE INDEX IF NOT EXISTS idx_results_started ON request_results(started_at);
CREATE INDEX IF NOT EXISTS idx_results_run_endpoint ON request_results(run_id, endpoint_id);
CREATE TABLE IF NOT EXISTS endpoint_metrics (
    run_id TEXT NOT NULL REFERENCES benchmark_runs(run_id) ON DELETE CASCADE,
    endpoint_id TEXT NOT NULL,
    endpoint_name TEXT,
    method TEXT,
    category TEXT,
    samples INTEGER, total INTEGER, successful INTEGER, failed INTEGER, timeouts INTEGER,
    rate_limited INTEGER, success_rate REAL, failure_rate REAL,
    p50_ms REAL, p95_ms REAL, p99_ms REAL, min_ms REAL, max_ms REAL, mean_ms REAL, std_ms REAL,
    rps REAL, p95_reliable INTEGER, p99_reliable INTEGER,
    warnings_json TEXT,
    computed_at TEXT,
    PRIMARY KEY (run_id, endpoint_id)
);
CREATE TABLE IF NOT EXISTS ws_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES benchmark_runs(run_id) ON DELETE CASCADE,
    metric TEXT NOT NULL,
    value REAL,
    recorded_at TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_ws_run_metric ON ws_samples(run_id, metric);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS order_test_orders (
    norenordno TEXT PRIMARY KEY,
    run_id TEXT,
    exch TEXT,
    tsym TEXT,
    placed_at TEXT,
    started_by TEXT,
    state TEXT NOT NULL DEFAULT 'open',
    final_status TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_order_test_state ON order_test_orders(state);
"""


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with microseconds and a trailing ``Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_run_id(prefix: str = "run") -> str:
    """Return ``f"{prefix}-{UTC yyyymmdd-HHMMSS}-{6 hex}"``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


def _none_if_nan(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):  # numpy scalar -> python
        try:
            return value.item()
        except (AttributeError, ValueError):
            return value
    return value


def _as_int_bool(value: Any) -> int:
    value = _none_if_nan(value)
    if isinstance(value, str):
        return int(value.strip().lower() in ("1", "true", "yes", "y", "t"))
    return int(bool(value))


def _as_dict(obj: Any) -> dict:
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "to_row"):
        return dict(obj.to_row())
    if hasattr(obj, "to_dict"):
        return dict(obj.to_dict())
    raise TypeError(f"Unsupported object for persistence: {type(obj).__name__}")


class Database:
    """Thin SQLite wrapper; every public method opens and closes its own connection."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _query_df(self, sql: str, params: Iterable[Any] = ()) -> pd.DataFrame:
        with self._connect() as conn:
            cur = conn.execute(sql, tuple(params))
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return pd.DataFrame([tuple(r) for r in rows], columns=cols)

    # ------------------------------------------------------------------ schema
    def init_schema(self) -> None:
        """Create tables and indexes if missing (idempotent) and enable WAL journaling."""
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    # --------------------------------------------------------------- endpoints
    def upsert_endpoints(self, endpoints: list[Endpoint]) -> None:
        """Insert or update endpoint definitions (objects with ``to_dict()`` or plain dicts)."""
        now = utc_now_iso()
        rows = []
        for ep in endpoints:
            d = ep.to_dict() if hasattr(ep, "to_dict") else dict(ep)
            rows.append(
                (
                    d.get("id"), d.get("name"), d.get("category"), d.get("method", "POST"),
                    d.get("path", ""), d.get("safety_class", "read_only"),
                    json.dumps(d, default=str), now,
                )
            )
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO endpoints (id, name, category, method, path, safety_class, definition_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, category=excluded.category,
                     method=excluded.method, path=excluded.path, safety_class=excluded.safety_class,
                     definition_json=excluded.definition_json, updated_at=excluded.updated_at""",
                rows,
            )

    def list_endpoints(self) -> pd.DataFrame:
        """Stored endpoint definitions (without the JSON blob)."""
        return self._query_df(
            "SELECT id, name, category, method, path, safety_class, updated_at FROM endpoints ORDER BY id"
        )

    # -------------------------------------------------------------------- runs
    def create_run(
        self,
        *,
        run_id: str,
        run_type: str,
        environment: str,
        base_host: str,
        connection_mode: str,
        config: dict,
        planned_requests: int,
        is_synthetic: bool = False,
        notes: str = "",
        started_at: str | None = None,
    ) -> str:
        """Insert a ``running`` benchmark run and return its id.

        ``started_at`` (UTC ISO) defaults to now; demo data uses it to back-date runs.
        """
        if run_type not in RUN_TYPES:
            raise ValueError(f"run_type must be one of {RUN_TYPES}, got {run_type!r}")
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO benchmark_runs (run_id, run_type, created_at, started_at, status, is_synthetic,
                     environment, base_host, connection_mode, config_json, notes, planned_requests, app_version)
                   VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, run_type, now, started_at or now, int(bool(is_synthetic)), environment,
                    base_host, connection_mode, json.dumps(config or {}, default=str),
                    redact_text(notes, registered_secrets()) if notes else "",
                    int(planned_requests), APP_VERSION,
                ),
            )
        return run_id

    def finish_run(
        self, run_id: str, status: str, stop_reason: str | None = None, completed_at: str | None = None
    ) -> None:
        """Mark a run finished with ``status`` (completed/stopped/failed) and optional reason."""
        if status not in RUN_STATUSES:
            raise ValueError(f"status must be one of {RUN_STATUSES}, got {status!r}")
        reason = redact_text(stop_reason) if stop_reason else stop_reason
        with self._connect() as conn:
            conn.execute(
                "UPDATE benchmark_runs SET status=?, stop_reason=?, completed_at=? WHERE run_id=?",
                (status, reason, completed_at or utc_now_iso(), run_id),
            )

    def list_runs(self, include_synthetic: bool = True, run_types: list[str] | None = None) -> pd.DataFrame:
        """Runs newest first, with ``request_count`` (non-warmup) and ``endpoint_count`` columns."""
        where, params = [], []
        if not include_synthetic:
            where.append("r.is_synthetic = 0")
        if run_types:
            where.append(f"r.run_type IN ({','.join('?' * len(run_types))})")
            params.extend(run_types)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        df = self._query_df(
            f"""SELECT r.*,
                   (SELECT COUNT(*) FROM request_results q WHERE q.run_id = r.run_id AND q.is_warmup = 0) AS request_count,
                   (SELECT COUNT(DISTINCT q.endpoint_id) FROM request_results q WHERE q.run_id = r.run_id) AS endpoint_count
                FROM benchmark_runs r {clause}
                ORDER BY COALESCE(r.started_at, r.created_at) DESC, r.created_at DESC, r.rowid DESC""",
            params,
        )
        if len(df):
            df["is_synthetic"] = df["is_synthetic"].astype(bool)
        return df

    def get_run(self, run_id: str) -> dict | None:
        """Run metadata as a dict with ``config`` parsed from JSON, or ``None`` if unknown."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM benchmark_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        run = dict(row)
        try:
            run["config"] = json.loads(run.get("config_json") or "{}")
        except json.JSONDecodeError:
            run["config"] = {}
        run["is_synthetic"] = bool(run.get("is_synthetic"))
        return run

    def delete_run(self, run_id: str) -> None:
        """Delete a run; results, metrics and ws samples cascade."""
        with self._connect() as conn:
            conn.execute("DELETE FROM benchmark_runs WHERE run_id=?", (run_id,))

    def delete_synthetic_runs(self) -> int:
        """Delete every run flagged ``is_synthetic`` (cascading); returns the number deleted."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM benchmark_runs WHERE is_synthetic = 1")
            return int(cur.rowcount)

    # ----------------------------------------------------------------- results
    def insert_results(self, results: Iterable[RequestResult | dict]) -> int:
        """Insert request results (``RequestResult`` via ``to_row()`` or dicts); returns row count."""
        secrets = registered_secrets()
        rows = []
        for r in results:
            d = _as_dict(r)
            source = d.get("source") or "measured"
            if "is_synthetic" not in d or _none_if_nan(d.get("is_synthetic")) is None:
                d["is_synthetic"] = source == "synthetic"
            d["source"] = source
            for col in _RESULT_BOOL_COLUMNS:
                d[col] = _as_int_bool(d.get(col))
            if d.get("error_message"):
                d["error_message"] = redact_text(str(d["error_message"]), secrets)
            rows.append(tuple(_none_if_nan(d.get(c)) for c in RESULT_COLUMNS))
        if not rows:
            return 0
        placeholders = ",".join("?" * len(RESULT_COLUMNS))
        with self._connect() as conn:
            conn.executemany(
                f"INSERT INTO request_results ({','.join(RESULT_COLUMNS)}) VALUES ({placeholders})", rows
            )
        return len(rows)

    def get_results(
        self,
        run_ids: list[str] | None = None,
        endpoint_ids: list[str] | None = None,
        include_warmup: bool = False,
        include_synthetic: bool = True,
    ) -> pd.DataFrame:
        """Request-level rows (``id`` + ``RequestResult.to_row`` columns + ``is_synthetic``).

        Boolean columns (is_warmup, success, is_timeout, is_synthetic) are Python ``bool``.
        """
        where, params = [], []
        if run_ids is not None:
            if not run_ids:
                return pd.DataFrame(columns=["id", *RESULT_COLUMNS])
            where.append(f"run_id IN ({','.join('?' * len(run_ids))})")
            params.extend(run_ids)
        if endpoint_ids is not None:
            if not endpoint_ids:
                return pd.DataFrame(columns=["id", *RESULT_COLUMNS])
            where.append(f"endpoint_id IN ({','.join('?' * len(endpoint_ids))})")
            params.extend(endpoint_ids)
        if not include_warmup:
            where.append("is_warmup = 0")
        if not include_synthetic:
            where.append("is_synthetic = 0")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        df = self._query_df(
            f"SELECT id, {','.join(RESULT_COLUMNS)} FROM request_results {clause} ORDER BY started_at, id",
            params,
        )
        for col in _RESULT_BOOL_COLUMNS:
            df[col] = df[col].astype(bool)
        df["latency_ms"] = pd.to_numeric(df["latency_ms"], errors="coerce").astype(float)
        df["http_status"] = pd.to_numeric(df["http_status"], errors="coerce").astype("Int64")
        return df

    # ----------------------------------------------------------------- metrics
    def save_endpoint_metrics(self, run_id: str, metrics: pd.DataFrame) -> None:
        """Replace stored endpoint metrics for ``run_id`` with the rows of ``metrics``."""
        now = utc_now_iso()
        rows = []
        if metrics is not None and len(metrics):
            for rec in metrics.to_dict("records"):
                values = []
                for col in _METRIC_STORE_COLUMNS:
                    v = _none_if_nan(rec.get(col))
                    if col in ("p95_reliable", "p99_reliable"):
                        v = _as_int_bool(v)
                    values.append(v)
                warnings = rec.get("warnings")
                if not isinstance(warnings, (list, tuple)):
                    warnings = [] if _none_if_nan(warnings) is None else [str(warnings)]
                rows.append((run_id, *values, json.dumps(list(warnings)), now))
        cols = ["run_id", *_METRIC_STORE_COLUMNS, "warnings_json", "computed_at"]
        with self._connect() as conn:
            conn.execute("DELETE FROM endpoint_metrics WHERE run_id=?", (run_id,))
            if rows:
                conn.executemany(
                    f"INSERT INTO endpoint_metrics ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    rows,
                )

    def recompute_metrics(self, run_id: str) -> pd.DataFrame:
        """Recompute endpoint metrics from stored rows (categories from ``endpoints``), save, return."""
        df = self.get_results([run_id], include_warmup=True)
        if len(df):
            cats = self._query_df("SELECT id AS endpoint_id, category FROM endpoints")
            df = df.merge(cats, on="endpoint_id", how="left") if len(cats) else df.assign(category=None)
        result = metrics_mod.endpoint_metrics(df)
        self.save_endpoint_metrics(run_id, result)
        return result

    def get_metrics(self, run_ids: list[str] | None = None) -> pd.DataFrame:
        """Stored endpoint metrics joined with run info (``run_started_at``, ``run_type``,
        ``run_is_synthetic``); ``warnings`` is a list and reliability flags are bools."""
        params: list[Any] = []
        clause = ""
        if run_ids is not None:
            if not run_ids:
                return pd.DataFrame(columns=["run_id", *metrics_mod.METRIC_COLUMNS, "computed_at"])
            clause = f"WHERE m.run_id IN ({','.join('?' * len(run_ids))})"
            params.extend(run_ids)
        df = self._query_df(
            f"""SELECT m.*, r.started_at AS run_started_at, r.run_type AS run_type,
                       r.is_synthetic AS run_is_synthetic
                FROM endpoint_metrics m JOIN benchmark_runs r ON r.run_id = m.run_id {clause}
                ORDER BY r.started_at DESC, m.endpoint_id""",
            params,
        )
        df["warnings"] = [json.loads(w) if w else [] for w in df["warnings_json"]] if len(df) else []
        df = df.drop(columns=["warnings_json"])
        for col in ("p95_reliable", "p99_reliable", "run_is_synthetic"):
            df[col] = df[col].astype(bool)
        return df

    # ---------------------------------------------------------------- websocket
    def insert_ws_samples(self, run_id: str, samples: Iterable[tuple[str, float, str, str | None]]) -> None:
        """Insert WebSocket samples as ``(metric, value, recorded_at, detail)`` tuples."""
        rows = [
            (run_id, m, _none_if_nan(v), ts, redact_text(d) if d else d) for (m, v, ts, d) in samples
        ]
        if rows:
            with self._connect() as conn:
                conn.executemany(
                    "INSERT INTO ws_samples (run_id, metric, value, recorded_at, detail) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )

    def get_ws_samples(self, run_id: str) -> pd.DataFrame:
        """WebSocket samples for a run, ordered by insertion."""
        return self._query_df(
            "SELECT id, run_id, metric, value, recorded_at, detail FROM ws_samples WHERE run_id=? ORDER BY id",
            (run_id,),
        )

    # --------------------------------------------------------- order test journal
    def record_test_orders(self, rows: Iterable[dict]) -> None:
        """Journal live test orders the moment they are accepted, so a crash can't lose track of them."""
        now = utc_now_iso()
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO order_test_orders "
                "(norenordno, run_id, exch, tsym, placed_at, started_by, state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
                [(r["norenordno"], r.get("run_id"), r.get("exch"), r.get("tsym"), r.get("placed_at") or now,
                  r.get("started_by"), now) for r in rows])

    def update_test_order(self, norenordno: str, state: str, final_status: str | None = None) -> None:
        """``state``: open (not confirmed closed), closed (cancelled/rejected), filled."""
        with self._connect() as conn:
            conn.execute("UPDATE order_test_orders SET state = ?, final_status = ?, updated_at = ? "
                         "WHERE norenordno = ?", (state, final_status, utc_now_iso(), norenordno))

    def unresolved_test_orders(self) -> list[dict]:
        """Test orders not yet confirmed cancelled, rejected or filled."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM order_test_orders WHERE state = 'open' ORDER BY placed_at").fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        """Return a non-secret app setting (JSON-decoded) or ``default``."""
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM app_settings WHERE key=?", (key,)).fetchone()
        if row is None or row[0] is None:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        """Store a non-secret setting. Raises ``ValueError`` for secret-looking keys or values."""
        lowered = key.lower()
        if any(hint in lowered for hint in _SECRET_KEY_HINTS):
            raise ValueError(f"Refusing to store secret-looking setting key {key!r}; use .env instead")
        encoded = json.dumps(value, default=str)
        for secret in registered_secrets():
            if secret and (secret in encoded or value == secret):
                raise ValueError(f"Refusing to store a registered secret value under {key!r}")
        if isinstance(value, str) and value.lower().startswith("bearer "):
            raise ValueError(f"Refusing to store a bearer token under {key!r}")
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (key, encoded, utc_now_iso()),
            )


def get_database(settings: Settings | None = None) -> Database:
    """Open (creating directories/schema as needed) the database at ``settings.db_path``."""
    settings = settings or load_settings()
    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return Database(path)
