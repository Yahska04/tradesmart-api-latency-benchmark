"""TradeSmart script master: download, store and search the public instrument lists.

TradeSmart publishes one zipped CSV per exchange (``config/endpoints.json`` → ``script_master``),
e.g. ``https://v2api.tradesmartonline.in/NSE_symbols.txt.zip``. The files are public: no credentials
are sent. Observed headers (2026-10):

- NSE / BSE: ``Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,``
- NFO:  ``Exchange,Token,LotSize,Symbol,TradingSymbol,Expiry,Instrument,OptionType,StrikePrice,TickSize,``
- BFO uses ``Strike``; CDS/BCD/MCX add ``Precision``, ``Multiplier``, ``GNGD``, ``PriceUnits`` … (ignored).

Columns are mapped by header name (case-insensitive, aliases below); unknown columns are ignored.
Rows are stored in SQLite at ``<folder of TS_DB_PATH>/scriptmaster/instruments.db``; each exchange is
replaced atomically, so a failed download keeps the previous data. Downloads happen at most once per
IST calendar day per exchange (``force=True`` overrides).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import threading
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx

from src import outbound
from src.redaction import LOGGER_NAME
from src.settings import PROJECT_ROOT

logger = logging.getLogger(LOGGER_NAME)

IST = timezone(timedelta(hours=5, minutes=30), "IST")
REGISTRY_PATH: Path = PROJECT_ROOT / "config" / "endpoints.json"
DOWNLOAD_TIMEOUT_S = 60.0
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024          # compressed zip
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024     # all .txt members together (zip-bomb guard)
DB_FILENAME = "instruments.db"
# Exchange preference when ranking equally good search matches.
SEARCH_PRIORITY: tuple[str, ...] = ("NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "BCD")
DERIVATIVE_EXCHANGES: tuple[str, ...] = ("NFO", "BFO", "CDS", "BCD", "MCX")
EQUITY_EXCHANGES: tuple[str, ...] = ("NSE", "BSE")

# normalised header (lowercase, alphanumerics only) -> Instrument field
_HEADER_ALIASES: dict[str, str] = {
    "exchange": "exch", "exch": "exch", "exchseg": "exch",
    "token": "token", "scripcode": "token",
    "tradingsymbol": "tsym", "tsym": "tsym",
    "symbol": "symbol", "symname": "symbol",
    "instrument": "instrument", "instname": "instrument", "instrumenttype": "instrument",
    "lotsize": "lot_size", "ls": "lot_size",
    "ticksize": "tick_size", "ti": "tick_size",
    "expiry": "expiry", "expirydate": "expiry", "exd": "expiry",
    "optiontype": "option_type", "optt": "option_type",
    "strikeprice": "strike", "strike": "strike", "strprc": "strike",
}
_OPTION_TYPES = {"CE", "PE"}

_state_lock = threading.Lock()      # guards _thread / _progress
_write_lock = threading.Lock()      # one refresh writes at a time
_thread: threading.Thread | None = None
_progress: dict[str, Any] = {"running": False, "current": None, "done": 0, "total": 0,
                             "started_at": None, "finished_at": None, "last_results": None}
_storage_dir: Path | None = None
_schema_ready: set[str] = set()


class ScriptMasterError(RuntimeError):
    """A download or parse failure for one exchange."""


@dataclass(frozen=True)
class Instrument:
    exch: str
    token: str
    tsym: str
    symbol: str
    instrument: str
    lot_size: int
    tick_size: float
    expiry: str | None = None
    strike: float | None = None
    option_type: str | None = None

    @property
    def label(self) -> str:
        """``"NSE · SBIN-EQ (3045)"``."""
        return f"{self.exch} · {self.tsym} ({self.token})"

    @property
    def is_derivative(self) -> bool:
        return bool(self.expiry or self.option_type)

    @property
    def key(self) -> str:
        """Stable identifier ``"EXCH|token"`` (handy as a widget option)."""
        return f"{self.exch}|{self.token}"


# ============================================================================ storage location
def set_storage_dir(path: Path | str | None) -> None:
    """Use ``path`` for ``instruments.db`` (``None`` = default next to ``TS_DB_PATH``)."""
    global _storage_dir
    _storage_dir = Path(path) if path is not None else None


def storage_dir() -> Path:
    if _storage_dir is not None:
        return _storage_dir
    from src.settings import load_settings

    return load_settings().db_path.parent / "scriptmaster"


def db_file() -> Path:
    return storage_dir() / DB_FILENAME


def _connect() -> sqlite3.Connection:
    path = db_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    key = str(path.resolve())
    if key not in _schema_ready:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS instruments (
                exch TEXT NOT NULL, token TEXT NOT NULL, tsym TEXT NOT NULL, symbol TEXT,
                instrument TEXT, lot_size INTEGER, tick_size REAL, expiry TEXT, expiry_iso TEXT,
                strike REAL, option_type TEXT, tsym_u TEXT NOT NULL, symbol_u TEXT, search_u TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_instr_exch_tsym ON instruments(exch, tsym);
            CREATE INDEX IF NOT EXISTS ix_instr_exch_token ON instruments(exch, token);
            CREATE INDEX IF NOT EXISTS ix_instr_tsym_u ON instruments(tsym_u);
            CREATE INDEX IF NOT EXISTS ix_instr_symbol_u ON instruments(symbol_u);
            CREATE TABLE IF NOT EXISTS meta (
                exch TEXT PRIMARY KEY, downloaded_at TEXT, rows INTEGER, source_url TEXT,
                ok INTEGER, error TEXT, checked_at TEXT
            );
            """
        )
        _schema_ready.add(key)
    return conn


# ============================================================================ sources
def sources(path: Path | None = None) -> dict[str, dict[str, str]]:
    """``{"NSE": {"url": ..., "segment": ...}, ...}`` from ``config/endpoints.json`` (config order)."""
    try:
        raw = json.loads((path or REGISTRY_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Script master: couldn't read sources (%s)", exc)
        return {}
    out: dict[str, dict[str, str]] = {}
    for item in raw.get("script_master") or []:
        exch = str(item.get("exchange") or "").strip().upper()
        url = str(item.get("url") or "").strip()
        if exch and url:
            out[exch] = {"url": url, "segment": str(item.get("segment") or exch)}
    return out


# ============================================================================ time helpers
def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_ist(iso: str | None) -> datetime | None:
    """Parse a stored UTC ISO timestamp and convert it to IST (``None`` if missing/invalid)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def _ist_date(iso: str | None) -> date | None:
    dt = to_ist(iso)
    return dt.date() if dt else None


# ============================================================================ parsing
def _norm_header(name: str) -> str:
    return "".join(ch for ch in name.strip().lower() if ch.isalnum())


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=4096)
def _expiry_iso(expiry: str | None) -> str | None:
    if not expiry:
        return None
    for fmt in ("%d-%b-%Y", "%d%b%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(expiry.strip().title(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_symbols_text(text: str, exch: str) -> list[Instrument]:
    """Parse one exchange's CSV text by its header row. Rows without token/tsym are skipped."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    header: list[str] | None = None
    for row in reader:
        if any(cell.strip() for cell in row):
            header = row
            break
    if header is None:
        raise ScriptMasterError("the file is empty")
    index: dict[str, int] = {}
    for i, name in enumerate(header):
        field_name = _HEADER_ALIASES.get(_norm_header(name))
        if field_name and field_name not in index:
            index[field_name] = i
    missing = [f for f in ("token", "tsym") if f not in index]
    if missing:
        raise ScriptMasterError(f"unexpected header (no {', '.join(missing)} column): "
                                f"{','.join(h.strip() for h in header)[:200]}")

    def cell(row: list[str], name: str) -> str:
        i = index.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    exch = exch.upper()
    out: list[Instrument] = []
    for row in reader:
        token, tsym = cell(row, "token"), cell(row, "tsym")
        if not token or not tsym:
            continue
        opt = cell(row, "option_type").upper()
        option_type = opt if opt in _OPTION_TYPES else None
        strike = _to_float(cell(row, "strike")) if option_type else None
        out.append(Instrument(
            exch=exch, token=token, tsym=tsym, symbol=cell(row, "symbol") or tsym,
            instrument=cell(row, "instrument"), lot_size=_to_int(cell(row, "lot_size"), 0),
            tick_size=_to_float(cell(row, "tick_size")) or 0.0,
            expiry=cell(row, "expiry") or None, strike=strike, option_type=option_type,
        ))
    return out


def extract_text(content: bytes, max_uncompressed: int | None = None) -> str:
    """Return the concatenated ``.txt`` members of a zip held in memory (size-capped)."""
    cap = MAX_UNCOMPRESSED_BYTES if max_uncompressed is None else max_uncompressed
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ScriptMasterError(f"not a valid zip file ({exc})") from exc
    members = [i for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(".txt")]
    if not members:
        raise ScriptMasterError("the zip contains no .txt file")
    if sum(i.file_size for i in members) > cap:
        raise ScriptMasterError(f"uncompressed size exceeds the {cap:,}-byte limit")
    parts: list[str] = []
    total = 0
    for info in members:
        buf = bytearray()
        with zf.open(info) as fh:
            while chunk := fh.read(1024 * 1024):
                total += len(chunk)
                if total > cap:  # header sizes can lie; count what is actually inflated
                    raise ScriptMasterError(f"uncompressed size exceeds the {cap:,}-byte limit")
                buf.extend(chunk)
        parts.append(bytes(buf).decode("utf-8-sig", errors="replace"))
    return "\n".join(parts) if len(parts) == 1 else _merge_parts(parts)


def _merge_parts(parts: list[str]) -> str:
    """Several .txt members: keep the first header, drop identical headers of later members."""
    first = parts[0]
    header = first.splitlines()[0] if first else ""
    merged = [first.rstrip("\r\n")]
    for part in parts[1:]:
        lines = part.splitlines()
        if lines and lines[0] == header:
            lines = lines[1:]
        merged.append("\n".join(lines))
    return "\n".join(merged)


# ============================================================================ download + store
def _download(client: httpx.Client, url: str) -> bytes:
    # Public file: no Authorization header, no cookies, no account data.
    with client.stream("GET", url, timeout=DOWNLOAD_TIMEOUT_S) as resp:
        if resp.status_code != 200:
            raise ScriptMasterError(f"HTTP {resp.status_code} from {httpx.URL(url).host}")
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf.extend(chunk)
            if len(buf) > MAX_DOWNLOAD_BYTES:
                raise ScriptMasterError(f"download exceeds the {MAX_DOWNLOAD_BYTES:,}-byte limit")
    return bytes(buf)


def _row(inst: Instrument) -> tuple:
    strike_txt = f"{inst.strike:g}" if inst.strike is not None else ""
    search = " ".join(x for x in (inst.tsym, inst.symbol, inst.instrument, inst.expiry or "",
                                  inst.option_type or "", strike_txt) if x).upper()
    return (inst.exch, inst.token, inst.tsym, inst.symbol, inst.instrument, inst.lot_size,
            inst.tick_size, inst.expiry, _expiry_iso(inst.expiry), inst.strike, inst.option_type,
            inst.tsym.upper(), (inst.symbol or "").upper(), search)


def store_instruments(exch: str, instruments: Sequence[Instrument], *, source_url: str = "",
                      now: datetime | None = None) -> int:
    """Atomically replace one exchange's rows and mark it downloaded."""
    exch = exch.upper()
    stamp = _iso(_utc_now(now))
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM instruments WHERE exch = ?", (exch,))
            conn.executemany(
                "INSERT INTO instruments (exch, token, tsym, symbol, instrument, lot_size, tick_size, "
                "expiry, expiry_iso, strike, option_type, tsym_u, symbol_u, search_u) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_row(i) for i in instruments))
            conn.execute(
                "INSERT INTO meta (exch, downloaded_at, rows, source_url, ok, error, checked_at) "
                "VALUES (?,?,?,?,1,NULL,?) ON CONFLICT(exch) DO UPDATE SET downloaded_at=excluded.downloaded_at, "
                "rows=excluded.rows, source_url=excluded.source_url, ok=1, error=NULL, "
                "checked_at=excluded.checked_at",
                (exch, stamp, len(instruments), source_url, stamp))
    finally:
        conn.close()
    return len(instruments)


def _record_failure(exch: str, source_url: str, error: str, now: datetime | None) -> None:
    """Keep previous rows/downloaded_at; only flag the failed attempt."""
    stamp = _iso(_utc_now(now))
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO meta (exch, downloaded_at, rows, source_url, ok, error, checked_at) "
                "VALUES (?,NULL,0,?,0,?,?) ON CONFLICT(exch) DO UPDATE SET ok=0, error=excluded.error, "
                "source_url=excluded.source_url, checked_at=excluded.checked_at",
                (exch, source_url, error[:500], stamp))
    finally:
        conn.close()


def _meta() -> dict[str, dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT exch, downloaded_at, rows, source_url, ok, error, checked_at FROM meta").fetchall()
    finally:
        conn.close()
    return {r["exch"]: dict(r) for r in rows}


def refresh(exchanges: Sequence[str] | None = None, *, force: bool = False,
            client: httpx.Client | None = None, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Download, parse and store each exchange's script master.

    Skips exchanges already downloaded successfully on the current IST date unless ``force``.
    One failed exchange never stops the others; its previous rows are kept. No retries.
    Returns ``{exch: {"ok", "skipped", "rows", "error", "url"}}``.
    """
    srcs = sources()
    wanted = [e.strip().upper() for e in (exchanges or list(srcs)) if e and e.strip()]
    current = _utc_now(now)
    today = current.astimezone(IST).date()
    results: dict[str, dict[str, Any]] = {}
    own_client = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_S,
                                  **outbound.httpx_transport_kwargs(),
                                  headers={"User-Agent": "tsbench-script-master/1.0"})
    with _write_lock:
        _set_progress(running=True, current=None, done=0, total=len(wanted),
                      started_at=_iso(current), finished_at=None)
        try:
            meta = _meta()
            for n, exch in enumerate(wanted):
                _set_progress(current=exch, done=n)
                src = srcs.get(exch)
                if src is None:
                    results[exch] = {"ok": False, "skipped": False, "rows": 0, "url": None,
                                     "error": "exchange not listed in config/endpoints.json script_master"}
                    continue
                m = meta.get(exch) or {}
                if not force and m.get("ok") and _ist_date(m.get("downloaded_at")) == today:
                    results[exch] = {"ok": True, "skipped": True, "rows": int(m.get("rows") or 0),
                                     "error": None, "url": src["url"]}
                    continue
                try:
                    content = _download(http, src["url"])
                    instruments = parse_symbols_text(extract_text(content), exch)
                    if not instruments:
                        raise ScriptMasterError("the file has no instrument rows")
                    rows = store_instruments(exch, instruments, source_url=src["url"], now=current)
                    results[exch] = {"ok": True, "skipped": False, "rows": rows, "error": None,
                                     "url": src["url"]}
                    logger.info("Script master: %s refreshed (%d instruments)", exch, rows)
                except Exception as exc:  # one exchange must not stop the others
                    msg = _error_text(exc)
                    results[exch] = {"ok": False, "skipped": False, "rows": int(m.get("rows") or 0),
                                     "error": msg, "url": src["url"]}
                    logger.warning("Script master: %s download failed (%s); previous data kept", exch, msg)
                    try:
                        _record_failure(exch, src["url"], msg, current)
                    except Exception as db_exc:  # pragma: no cover - storage unavailable
                        logger.warning("Script master: couldn't record failure for %s (%s)", exch, db_exc)
        finally:
            if own_client:
                http.close()
            _set_progress(running=False, current=None, done=len(wanted),
                          finished_at=_iso(datetime.now(timezone.utc)), last_results=results)
    return results


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ScriptMasterError):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return f"timed out after {DOWNLOAD_TIMEOUT_S:g}s"
    if isinstance(exc, httpx.HTTPError):
        return f"network error ({type(exc).__name__}: {exc})"[:300]
    return f"{type(exc).__name__}: {exc}"[:300]


# ============================================================================ background refresh
def _set_progress(**kw: Any) -> None:
    with _state_lock:
        _progress.update(kw)


def is_refreshing() -> bool:
    with _state_lock:
        return bool(_progress["running"]) or (_thread is not None and _thread.is_alive())


def start_background_refresh(exchanges: Sequence[str] | None = None, *, force: bool = False) -> None:
    """Run :func:`refresh` in a daemon thread. A call while one is already running is a no-op."""
    global _thread
    with _state_lock:
        if _thread is not None and _thread.is_alive():
            return
        wanted = list(exchanges) if exchanges else None

        def _target() -> None:
            try:
                refresh(wanted, force=force)
            except Exception as exc:  # never let the thread die noisily
                logger.warning("Script master: background refresh failed (%s)", _error_text(exc))
                _set_progress(running=False, last_results={"_error": _error_text(exc)})

        _progress["running"] = True  # visible as "downloading" before the thread gets going
        _thread = threading.Thread(target=_target, name="script-master-refresh", daemon=True)
        _thread.start()


# ============================================================================ status
def status() -> dict[str, Any]:
    """Overall state and per-exchange details for the UI."""
    srcs = sources()
    with _state_lock:
        progress = dict(_progress)
    running = is_refreshing()
    try:
        meta = _meta()
        storage_error = None
    except Exception as exc:
        meta, storage_error = {}, _error_text(exc)
    per: dict[str, dict[str, Any]] = {}
    for exch in list(srcs) + [e for e in meta if e not in srcs]:
        m = meta.get(exch) or {}
        per[exch] = {
            "segment": (srcs.get(exch) or {}).get("segment", exch),
            "rows": int(m.get("rows") or 0),
            "downloaded_at": m.get("downloaded_at"),
            "checked_at": m.get("checked_at"),
            "ok": None if not m else bool(m.get("ok")),
            "error": m.get("error"),
        }
    attempted = [p for p in per.values() if p["ok"] is not None]
    failed = [p for p in per.values() if p["ok"] is False]
    pending = [p for p in per.values() if p["ok"] is None]
    with_data = [p for p in per.values() if p["rows"] > 0]
    if running:
        state = "downloading"
    elif not attempted:
        state = "error" if storage_error else "idle"
    elif failed:
        state = "partial" if with_data else "error"
    elif pending:  # nothing failed; some exchanges simply haven't been fetched yet
        state = "pending"
    else:
        state = "ready"
    stamps = [p["downloaded_at"] for p in per.values() if p["downloaded_at"]]
    return {
        "state": state,
        "per_exchange": per,
        "updated_at": max(stamps) if stamps else None,
        "exchanges_ready": sum(1 for p in per.values() if p["ok"]),
        "exchanges_with_data": len(with_data),
        "total_rows": sum(p["rows"] for p in per.values()),
        "current": progress.get("current") if running else None,
        "done": progress.get("done"), "total": progress.get("total"),
        "storage": str(db_file()),
        "error": storage_error,
    }


def has_data(exchanges: Sequence[str] | None = None) -> bool:
    """True when at least one of ``exchanges`` (default: any) has stored instruments."""
    try:
        conn = _connect()
    except Exception:
        return False
    try:
        if exchanges:
            ex = [e.upper() for e in exchanges]
            sql = f"SELECT 1 FROM instruments WHERE exch IN ({','.join('?' * len(ex))}) LIMIT 1"
            return conn.execute(sql, ex).fetchone() is not None
        return conn.execute("SELECT 1 FROM instruments LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


# ============================================================================ queries
_COLS = "exch, token, tsym, symbol, instrument, lot_size, tick_size, expiry, strike, option_type"


def _inst(row: sqlite3.Row) -> Instrument:
    return Instrument(exch=row["exch"], token=row["token"], tsym=row["tsym"], symbol=row["symbol"] or "",
                      instrument=row["instrument"] or "", lot_size=int(row["lot_size"] or 0),
                      tick_size=float(row["tick_size"] or 0.0), expiry=row["expiry"],
                      strike=row["strike"], option_type=row["option_type"])


def search(query: str, exchanges: Sequence[str] | None = None, limit: int = 25) -> list[Instrument]:
    """Case-insensitive instrument search: exact tsym/symbol first, then prefix, then contains.

    Multi-word queries (``"NIFTY 25000 CE"``) require every word to appear in the trading symbol,
    symbol, expiry, option type or strike; the first word drives exact/prefix ranking. Ties prefer
    NSE, BSE, NFO…, then non-options, nearest expiry, strike and the shorter symbol.
    """
    words = [w for w in (query or "").upper().split() if w]
    if not words or limit <= 0:
        return []
    first, rest = words[0], words[1:]
    ex = [e.upper() for e in exchanges] if exchanges else []
    prio = list(dict.fromkeys(ex or list(SEARCH_PRIORITY)))
    prio_sql = "CASE exch " + " ".join(f"WHEN '{e}' THEN {i}" for i, e in enumerate(prio)
                                       if e.isalnum()) + f" ELSE {len(prio)} END"
    where = ["1=1"]
    params: list[Any] = []
    if ex:
        where.append(f"exch IN ({','.join('?' * len(ex))})")
        params.extend(ex)
    for w in rest:
        where.append("instr(search_u, ?) > 0")
        params.append(w)
    # Prefix match as a range on the upper-cased columns, so SQLite can use their indexes.
    hi = first + "\U0010ffff"
    prefix = "((tsym_u >= ? AND tsym_u < ?) OR (symbol_u >= ? AND symbol_u < ?))"
    prefix_params = [first, hi, first, hi]
    order = (f"rnk, {prio_sql}, (option_type IS NOT NULL), expiry_iso IS NOT NULL, expiry_iso, "
             "strike, length(tsym), tsym")
    rank = "CASE WHEN tsym_u = ? OR symbol_u = ? THEN 0 ELSE 1 END AS rnk"
    out: list[Instrument] = []
    seen: set[tuple[str, str]] = set()
    conn = _connect()
    try:
        # Phase 1: exact + prefix on the first word (index-friendly).
        sql = (f"SELECT {_COLS}, {rank} FROM instruments WHERE {' AND '.join(where)} AND {prefix} "
               f"ORDER BY {order} LIMIT ?")
        rows = conn.execute(sql, [first, first, *params, *prefix_params, limit]).fetchall()
        for r in rows:
            seen.add((r["exch"], r["token"]))
            out.append(_inst(r))
        if len(out) < limit:
            # Phase 2: contains (every word anywhere in the searchable text).
            sql = (f"SELECT {_COLS}, 2 AS rnk FROM instruments WHERE {' AND '.join(where)} "
                   f"AND instr(search_u, ?) > 0 AND NOT {prefix} ORDER BY {order} LIMIT ?")
            rows = conn.execute(sql, [*params, first, *prefix_params, limit - len(out)]).fetchall()
            for r in rows:
                if (r["exch"], r["token"]) not in seen:
                    out.append(_inst(r))
    except sqlite3.Error as exc:
        logger.warning("Script master search failed (%s)", exc)
        return []
    finally:
        conn.close()
    return out[:limit]


def lookup(exch: str, tsym: str) -> Instrument | None:
    """Exact trading-symbol lookup (case-insensitive)."""
    conn = _connect()
    try:
        row = conn.execute(f"SELECT {_COLS} FROM instruments WHERE exch = ? AND tsym_u = ? LIMIT 1",
                           (exch.strip().upper(), tsym.strip().upper())).fetchone()
    finally:
        conn.close()
    return _inst(row) if row else None


def by_token(exch: str, token: str) -> Instrument | None:
    conn = _connect()
    try:
        row = conn.execute(f"SELECT {_COLS} FROM instruments WHERE exch = ? AND token = ? LIMIT 1",
                           (exch.strip().upper(), str(token).strip())).fetchone()
    finally:
        conn.close()
    return _inst(row) if row else None


def instruments_count(exchanges: Iterable[str] | None = None) -> int:
    conn = _connect()
    try:
        if exchanges:
            ex = [e.upper() for e in exchanges]
            sql = f"SELECT COUNT(*) FROM instruments WHERE exch IN ({','.join('?' * len(ex))})"
            return int(conn.execute(sql, ex).fetchone()[0])
        return int(conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0])
    finally:
        conn.close()
