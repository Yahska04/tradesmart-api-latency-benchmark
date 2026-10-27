# TradeSmart API Latency Benchmark — Architecture & Module Contract

This file is the **binding interface contract** between modules. Every module must implement
exactly the public names/signatures below so independently-written parts integrate cleanly.
Private helpers are free. Python 3.13 (3.12-compatible code: no 3.13-only syntax). Type hints everywhere.
`from __future__ import annotations` at the top of each module.

Project root = `D:\AI Bootcamp\Projects\Latency` (this folder). Virtual env: `.venv` (already created;
run tools with `.venv/Scripts/python -m pytest ...`). Installed: streamlit, httpx, pandas, numpy, plotly,
pytest, python-dotenv, websockets, respx, pymupdf.

```text
app.py                         # Streamlit entry = Overview dashboard (section A)
requirements.txt  .env.example  .gitignore  README.md
.streamlit/config.toml         # brand theme
assets/                        # logo svg/png converted from docs/brand-source (PyMuPDF)
config/endpoints.json          # endpoint registry (source of truth = official docs)
src/__init__.py
src/settings.py                # env/.env + non-secret settings
src/redaction.py               # secret masking + logging setup
src/endpoint_registry.py       # Endpoint dataclass, load/validate/render payloads
src/safety.py                  # safety classes, guards, rate limiter, circuit breaker
src/auth.py                    # OAuth URL, checksum, GenAcsTok
src/api_client.py              # HTTPX client, jData encoding, timed requests
src/benchmark_engine.py        # config, plan, runner
src/metrics.py                 # percentiles & stats & run comparison
src/database.py                # SQLite persistence
src/exporters.py               # CSV export with whitelist + redaction
src/demo_data.py               # synthetic, clearly-labelled demo data
src/websocket_benchmark.py     # WebSocket benchmark module (separate metrics)
src/ui/__init__.py  src/ui/theme.py  src/ui/components.py  src/ui/state.py
views/1_Endpoint_Registry.py 2_Run_Benchmark.py 3_Performance_Analytics.py
      4_Historical_Comparison.py 5_Settings.py 6_Methodology.py 7_WebSocket_Benchmark.py 8_Endpoint_Check.py
tests/ conftest.py + test_*.py  (NEVER hit the network: use respx / httpx.MockTransport / fakes)
data/.gitkeep                  # benchmark.db and logs/ live here (gitignored)
```

---

## 1. `src/settings.py`

Env vars (read via python-dotenv from `.env` in project root, real env wins):

| Var | Default | Secret |
|---|---|---|
| `TS_BASE_URL` | `https://v2api.tradesmartonline.in/NorenWClientAPIv2` | no |
| `TS_WS_URL` | `wss://v2api.tradesmartonline.in/NorenWSAPI/` | no |
| `TS_OAUTH_URL` | `https://v2api.tradesmartonline.in/OAuthlogin/authorize/oauth` | no |
| `TS_APP_KEY` | "" | yes |
| `TS_SECRET_KEY` | "" | yes |
| `TS_CLIENT_ID` | "" | yes (account identifier → treat as sensitive) |
| `TS_REDIRECT_URL` | "" | no |
| `TS_ACCESS_TOKEN` | "" | yes |
| `TS_ENVIRONMENT` | `live` | no — one of `live`, `uat`, `mock` |
| `TS_STATIC_IP` | "" | no (informational; recorded in run metadata) |
| `TS_DB_PATH` | `data/benchmark.db` | no |
| `TS_LOG_PATH` | `data/logs/app.log` | no |

```python
PROJECT_ROOT: Path
SECRET_FIELDS: frozenset[str]  # {"app_key","secret_key","client_id","access_token"}

@dataclass
class Settings:
    base_url: str; ws_url: str; oauth_url: str
    app_key: str; secret_key: str; client_id: str; redirect_url: str; access_token: str
    environment: str          # "live" | "uat" | "mock"
    static_ip: str; db_path: Path; log_path: Path
    def is_live(self) -> bool
    def has_credentials(self) -> bool          # access_token and client_id present
    def secret_values(self) -> list[str]       # non-empty secret values (for redaction)
    def masked(self) -> dict[str, str]          # all fields, secrets masked via redaction.mask_secret

def load_settings(env_file: Path | None = None) -> Settings
```

## 2. `src/redaction.py`

```python
REDACTED = "***REDACTED***"
SENSITIVE_KEYS: frozenset[str]  # lowercase JSON/form keys: access_token, accesstoken, susertoken, token,
                                 # checksum, code, secret, secret_key, app_key, password, pwd, uid, actid,
                                 # apikey, api_key, authorization, client_id, accno, bankaccno, pan
def mask_secret(value: str | None, visible: int = 2) -> str   # "" -> "", short -> "****", else "ab****yz"
def redact_text(text: str | None, secrets: Iterable[str] = ()) -> str
    # removes: literal secret values; "Bearer <x>"; JSON "key": "value" and key=value for SENSITIVE_KEYS;
    # long digit runs (>=9 digits) that look like account numbers. Must be idempotent.
def redact_mapping(obj: Any, secrets: Iterable[str] = ()) -> Any   # deep copy, sensitive keys -> REDACTED
class RedactingFilter(logging.Filter): ...   # applies redact_text to record msg+args
def setup_logging(log_path: Path, secrets: Iterable[str] = (), level: int = logging.INFO) -> logging.Logger
    # logger name "tsbench"; rotating file handler + RedactingFilter; idempotent (no duplicate handlers)
def register_secrets(secrets: Iterable[str]) -> None   # adds to the global filter's secret set
```

## 3. `src/endpoint_registry.py`

`config/endpoints.json` shape:
```json
{
  "schema_version": 1,
  "source": "https://apidocs.tradesmartonline.in/v2/",
  "checked_at": "2026-10-16",
  "base_url": "https://v2api.tradesmartonline.in/NorenWClientAPIv2",
  "ws_url": "wss://v2api.tradesmartonline.in/NorenWSAPI/",
  "request_encoding": "jData",
  "rate_limits": [{"applies_to": ["GetQuotes","GetQuotesMultiple"], "per_second": 100, "per_minute": 1000}],
  "endpoints": [ { ...Endpoint fields... } ],
  "websocket": { "connect": {...}, "feeds": [...], "heartbeat_seconds": 30 }
}
```

```python
SAFETY_CLASSES = ("read_only", "state_changing", "special_setup")

@dataclass
class Param:
    name: str; type: str = "string"; required: bool = True; description: str = ""; example: str | None = None

@dataclass
class Endpoint:
    id: str                         # e.g. "UserDetails" (== path without slash)
    name: str                       # human: "User Details"
    category: str                   # "Authentication", "Orders", "Orders & Trades", "Holdings & Limits",
                                    # "Scrips / Market Information", "Market Data", ...
    method: str = "POST"
    path: str = ""                  # "/UserDetails"
    auth_required: bool = True
    required_headers: list[str] = field(default_factory=list)   # ["Authorization: Bearer <access_token>"]
    params: list[Param] = field(default_factory=list)
    request_body_template: dict[str, Any] = field(default_factory=dict)  # placeholders "{{ClientId}}", "{{exch}}"...
    response_format: str = ""       # short description
    safety_class: str = "read_only"
    requires_fixture: bool = False  # e.g. SingleOrdHist needs a norenordno
    fixture_notes: str = ""
    rate_limit_per_sec: float | None = None
    rate_limit_per_min: float | None = None
    benchmark_enabled: bool = True  # default selection for automated benchmark (always False for state_changing)
    doc_reference: str = ""         # section/anchor in official docs
    notes: str = ""
    @property
    def url_path(self) -> str
    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, d: dict) -> "Endpoint"

class RegistryError(ValueError): ...
DEFAULT_REGISTRY_PATH: Path  # PROJECT_ROOT / "config" / "endpoints.json"
def load_registry(path: Path | None = None) -> dict          # raw json, validated
def load_endpoints(path: Path | None = None) -> list[Endpoint]
def validate_endpoint(ep: Endpoint) -> list[str]              # list of problems ([] = valid)
def save_endpoints(endpoints: list[Endpoint], path: Path | None = None) -> None  # preserves other top-level keys
def import_endpoints_json(text: str) -> list[Endpoint]        # accepts full registry or bare list
def render_payload(ep: Endpoint, context: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict
    # substitutes "{{Key}}" placeholders from context (ClientId, exch, token, tsym, ...); overrides win;
    # raises RegistryError listing unresolved placeholders
def missing_placeholders(ep: Endpoint, context: dict[str, Any], overrides: dict | None = None) -> list[str]
```

## 4. `src/safety.py`

```python
READ_ONLY, STATE_CHANGING, SPECIAL_SETUP = "read_only", "state_changing", "special_setup"
NEVER_AUTO_EXECUTE: frozenset[str]  # PlaceOrder, ModifyOrder, CancelOrder, ExitSnoOrder, PlaceMultiOrder,
                                    # GenAcsTok (auth exchange, one-time code) + any id containing Place/Modify/Cancel/Exit
class UnsafeEndpointError(RuntimeError): ...
def classify(ep: Endpoint) -> str        # returns ep.safety_class but forces STATE_CHANGING for NEVER_AUTO_EXECUTE names
def is_auto_benchmarkable(ep: Endpoint) -> bool   # read_only and not in NEVER_AUTO_EXECUTE
def assert_safe_to_execute(ep: Endpoint, *, environment: str, state_changing_test_mode: bool = False,
                           user_confirmed: bool = False) -> None
    # read_only -> ok. special_setup -> ok only if user_confirmed. state_changing -> ALWAYS raise when
    # environment == "live"; otherwise requires environment in {"mock","uat"} AND test_mode AND confirmed.
def live_account_warning(environment: str) -> str | None

class RateLimiter:     # sliding windows for per-second and per-minute; thread-safe
    def __init__(self, per_second: float | None, per_minute: float | None, safety_factor: float = 0.5,
                 min_interval_s: float = 0.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None
    @property
    def effective_per_second(self) -> float | None    # floor(limit*safety_factor), >=1
    @property
    def effective_per_minute(self) -> float | None
    def acquire(self) -> float      # blocks until allowed; returns seconds waited
DEFAULT_UNDOCUMENTED_PER_SECOND = 5   # conservative cap when docs give no limit
def rate_limiter_for(ep: Endpoint, safety_factor: float = 0.5, min_interval_s: float = 0.0, **kw) -> RateLimiter

class CircuitBreaker:   # per endpoint
    def __init__(self, max_consecutive_failures: int = 5, stop_on_rate_limit: bool = True) -> None
    def record(self, outcome: str) -> None     # outcome from api_client.OUTCOMES
    @property
    def tripped(self) -> bool
    @property
    def reason(self) -> str | None
    @property
    def global_stop(self) -> bool   # True when a 429/rate_limited seen and stop_on_rate_limit
```

## 5. `src/auth.py`

```python
def build_authorize_url(oauth_url: str, app_key: str) -> str        # ...?client_id=<app_key>
def compute_checksum(app_key: str, secret_key: str, auth_code: str) -> str   # sha256 hex of concat
def extract_auth_code(redirected_url_or_code: str) -> str          # accepts full redirect URL or bare code
@dataclass
class TokenResult: ok: bool; access_token: str | None; error: str | None; raw_keys: list[str]
def generate_access_token(settings: Settings, auth_code: str, client: httpx.Client | None = None,
                          timeout: float = 10.0) -> TokenResult
    # POST {base}/GenAcsTok body "jData=" + json {"code","checksum"}; never logs secret/code/token
```

## 6. `src/api_client.py`

```python
OUTCOMES = ("success", "api_error", "http_error", "rate_limited", "timeout", "connect_error",
            "dns_error", "other_error")
SERVER_TIMING_HEADERS = ("server-timing", "x-response-time", "x-runtime", "x-processing-time")
def encode_jdata(payload: dict) -> str     # "jData=" + json.dumps(payload, separators=(",", ":"))
def classify_response(status: int, body: bytes) -> tuple[bool, str, str | None, str | None]
    # -> (success, outcome, api_stat, emsg). success requires 2xx AND (stat=="Ok" OR JSON list OR
    # Not_Ok whose emsg matches /no data/i — completed request with empty dataset). 429 -> rate_limited.
    # 2xx + Not_Ok (other) -> api_error. non-2xx -> http_error.

@dataclass
class RequestResult:
    run_id: str; endpoint_id: str; endpoint_name: str; path: str; method: str
    seq: int; is_warmup: bool; connection_mode: str      # "cold" | "warm"
    started_at: str; completed_at: str                   # UTC ISO-8601 with microseconds, "Z"
    latency_ms: float | None                             # perf_counter_ns based; None only if never sent
    http_status: int | None; success: bool; outcome: str; is_timeout: bool
    error_type: str | None; error_message: str | None    # redacted
    response_bytes: int; api_stat: str | None; server_timing: str | None
    source: str = "measured"                             # "measured" | "manual" | "synthetic"
    def to_row(self) -> dict

class TradeSmartClient:
    def __init__(self, base_url: str, access_token: str = "", timeout_s: float = 10.0,
                 connection_mode: str = "warm", transport: httpx.BaseTransport | None = None,
                 secrets: Iterable[str] = ()) -> None
        # warm: one persistent httpx.Client (keep-alive). cold: fresh httpx.Client per request (closed after)
        # so each sample includes DNS+TCP+TLS. transport is injected in tests.
    def timed_post(self, ep: Endpoint, payload: dict, *, run_id: str, seq: int, is_warmup: bool) -> RequestResult
        # t0=perf_counter_ns() immediately before send; t1 after full body read. Never raises for
        # network errors: maps httpx.TimeoutException->timeout, ConnectError w/ DNS hint (getaddrinfo,
        # Name or service not known, nodename) -> dns_error, other ConnectError -> connect_error.
        # Headers: Authorization: Bearer <token> when ep.auth_required. No automatic retries.
    def connectivity_check(self) -> dict    # one lightweight request? NO — only DNS resolve + TCP/TLS
                                             # handshake timing to base host (no API call). keys: ok, dns_ms,
                                             # connect_ms, error
    def close(self) -> None
    def __enter__/__exit__
```

## 7. `src/metrics.py`

```python
PERCENTILE_METHOD = "linear"          # numpy.percentile(..., method="linear")
MIN_SAMPLES_P95 = 20
MIN_SAMPLES_P99 = 100
METRIC_COLUMNS = ["endpoint_id","endpoint_name","method","category","samples","total","successful",
    "failed","timeouts","rate_limited","success_rate","failure_rate","p50_ms","p95_ms","p99_ms","min_ms",
    "max_ms","mean_ms","std_ms","rps","p95_reliable","p99_reliable","warnings"]

def percentile(values: Sequence[float], q: float) -> float | None   # None if empty
def summarize(df: pd.DataFrame) -> dict
    # input = request-level rows (columns per RequestResult.to_row). Excludes is_warmup rows.
    # Percentiles/min/max/mean/std ONLY from success rows' latency_ms; samples = that count.
    # std: ddof=1, None if samples<2. success_rate = successful/total (None if total==0).
    # failed = total - successful; timeouts = is_timeout count; rps = total / wall seconds between first
    # started_at and last completed_at (None if <2 rows or 0s). warnings list[str] for small samples
    # ("P99 based on N samples (< 100); treat as indicative only"), no successes, etc.
def endpoint_metrics(df: pd.DataFrame) -> pd.DataFrame     # one row per endpoint_id, METRIC_COLUMNS
def overall_metrics(df: pd.DataFrame) -> dict              # summarize over all non-warmup rows + "endpoints"
def safe_change(baseline: float | None, candidate: float | None) -> tuple[float | None, float | None]
    # (abs_diff, pct_diff); pct None when baseline is None/0/NaN; abs None if either missing
def compare_runs(baseline: pd.DataFrame, candidate: pd.DataFrame) -> pd.DataFrame
    # inputs = endpoint_metrics frames; outer join on endpoint_id; columns:
    # endpoint_id, endpoint_name, {p50,p95,p99}_{base,cand,abs,pct}, success_rate_{base,cand,abs,pct},
    # failure_rate_{base,cand,abs,pct}, status ("both","baseline_only","candidate_only")
def comparable_conditions(run_a: dict, run_b: dict) -> list[str]  # differences in connection_mode,
    # environment, base host, iterations, delay, timeout, concurrency -> human readable list
```

## 8. `src/database.py`

SQLite with `PRAGMA journal_mode=WAL`, `foreign_keys=ON`. New connection per operation is fine
(thread safety with Streamlit). Tables (all timestamps UTC ISO text):

- `endpoints(id TEXT PK, name, category, method, path, safety_class, definition_json, updated_at)`
- `benchmark_runs(run_id TEXT PK, run_type TEXT ['http','manual','demo','websocket'], created_at, started_at,
   completed_at, status TEXT ['running','completed','stopped','failed'], is_synthetic INT, environment,
   base_host, connection_mode, config_json, notes, planned_requests INT, stop_reason, app_version)`
- `request_results(id INTEGER PK AUTOINCREMENT, run_id FK→benchmark_runs ON DELETE CASCADE, endpoint_id,
   endpoint_name, path, method, seq INT, is_warmup INT, connection_mode, started_at, completed_at,
   latency_ms REAL, http_status INT, success INT, outcome, is_timeout INT, error_type, error_message,
   response_bytes INT, api_stat, server_timing, source, is_synthetic INT)`
   indexes: (run_id), (endpoint_id), (started_at), (run_id, endpoint_id)
- `endpoint_metrics(run_id, endpoint_id, <METRIC_COLUMNS numeric fields>, warnings_json, computed_at,
   PRIMARY KEY(run_id, endpoint_id))`
- `ws_samples(id INTEGER PK, run_id FK, metric TEXT, value REAL, recorded_at, detail TEXT)` index (run_id, metric)
- `app_settings(key TEXT PK, value_json TEXT, updated_at)` — `set_setting` raises ValueError if key or
   value looks secret (key contains token/secret/key/password/checksum/client_id, or value is a
   registered secret).

```python
def new_run_id(prefix: str = "run") -> str     # f"{prefix}-{UTC yyyymmdd-HHMMSS}-{6 hex}"
class Database:
    def __init__(self, path: Path | str) -> None        # ":memory:" not required; tests use tmp_path
    def init_schema(self) -> None                       # idempotent, called by __init__
    def upsert_endpoints(self, endpoints: list[Endpoint]) -> None
    def create_run(self, *, run_id: str, run_type: str, environment: str, base_host: str,
                   connection_mode: str, config: dict, planned_requests: int, is_synthetic: bool = False,
                   notes: str = "") -> str
    def finish_run(self, run_id: str, status: str, stop_reason: str | None = None) -> None
    def insert_results(self, results: Iterable[RequestResult | dict]) -> int
    def save_endpoint_metrics(self, run_id: str, metrics: pd.DataFrame) -> None
    def recompute_metrics(self, run_id: str) -> pd.DataFrame  # from stored rows via metrics.endpoint_metrics
    def list_runs(self, include_synthetic: bool = True, run_types: list[str] | None = None) -> pd.DataFrame
    def get_run(self, run_id: str) -> dict | None          # config parsed from json
    def get_results(self, run_ids: list[str] | None = None, endpoint_ids: list[str] | None = None,
                    include_warmup: bool = False, include_synthetic: bool = True) -> pd.DataFrame
    def get_metrics(self, run_ids: list[str] | None = None) -> pd.DataFrame
    def delete_run(self, run_id: str) -> None
    def insert_ws_samples(self, run_id: str, samples: Iterable[tuple[str, float, str, str | None]]) -> None
    def get_ws_samples(self, run_id: str) -> pd.DataFrame
    def get_setting(self, key: str, default: Any = None) -> Any
    def set_setting(self, key: str, value: Any) -> None
def get_database(settings: Settings | None = None) -> Database   # from settings.db_path, creates dirs
```

## 9. `src/exporters.py`

```python
EXPORT_RESULT_COLUMNS = ["run_id","endpoint_id","endpoint_name","method","path","seq","is_warmup",
   "connection_mode","started_at","completed_at","latency_ms","http_status","success","outcome",
   "is_timeout","error_type","error_message","response_bytes","api_stat","source","is_synthetic"]
def results_to_csv(df: pd.DataFrame, secrets: Iterable[str] = ()) -> bytes  # whitelist + redact text cols
def metrics_to_csv(df: pd.DataFrame, secrets: Iterable[str] = ()) -> bytes
def export_filename(kind: str, run_ids: list[str] | None = None) -> str   # e.g. tsbench_results_<...>.csv
```

## 10. `src/benchmark_engine.py`

```python
@dataclass
class BenchmarkConfig:
    endpoint_ids: list[str]
    iterations: int = 20                 # 1..1000
    warmup: int = 2                      # 0..20 (stored, excluded from metrics)
    timeout_s: float = 10.0
    delay_ms: int = 500                  # pause between consecutive requests
    connection_mode: str = "warm"        # "warm" | "cold"
    order: str = "round_robin"           # "round_robin" (interleaved, fairer) | "grouped"
    concurrency: int = 1                 # 1 = sequential (default). max 4.
    max_duration_s: float | None = None  # hard stop
    safety_factor: float = 0.5           # fraction of documented rate limit
    stop_on_rate_limit: bool = True
    max_consecutive_failures: int = 5    # per endpoint -> pause that endpoint
    payload_context: dict[str, Any] = field(default_factory=dict)            # ClientId, exch, token, ...
    payload_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)  # per endpoint id
    environment: str = "live"
    notes: str = ""
    def validate(self) -> list[str]
    def to_dict(self) -> dict            # NO secrets (payload_context values for ClientId etc. masked)

@dataclass
class BenchmarkPlan:
    total_requests: int; warmup_requests: int; measured_requests: int
    estimated_duration_s: float          # accounts for delay, rate limits and timeout-free typical latency guess
    per_endpoint: list[dict]             # endpoint_id, requests, effective_rate_limit
    blocked: list[dict]                  # endpoint_id, reason (unsafe / missing placeholders)
    warnings: list[str]

def build_plan(config: BenchmarkConfig, endpoints: list[Endpoint], typical_latency_ms: float = 150.0) -> BenchmarkPlan

@dataclass
class Progress:
    run_id: str; completed: int; total: int; current_endpoint: str | None
    last_result: RequestResult | None; elapsed_s: float; message: str = ""

class BenchmarkRunner:
    def __init__(self, db: "Database", endpoints: list[Endpoint], client_factory: Callable[[BenchmarkConfig], TradeSmartClient],
                 base_url: str, logger: logging.Logger | None = None) -> None
    def run(self, config: BenchmarkConfig, progress_cb: Callable[[Progress], None] | None = None,
            stop_event: threading.Event | None = None) -> str
        # 1 create_run(run_type="http"); 2 safety.assert_safe_to_execute per endpoint (blocked ones skipped
        # and recorded in stop_reason/notes); 3 warmups then iterations in configured order; each request
        # passes RateLimiter.acquire + delay; 4 results inserted in batches (flush every 25 and at end);
        # 5 CircuitBreaker: pause endpoint when tripped; global stop on rate_limited when configured;
        # 6 finish_run(status, stop_reason) and save_endpoint_metrics(metrics.endpoint_metrics(...)).
        # Never retries. Never raises for request errors; returns run_id.
def record_manual_result(db: "Database", ep: Endpoint, latency_ms: float, http_status: int | None,
                         success: bool, environment: str, notes: str = "", run_id: str | None = None) -> str
    # documentation-only/manual latency entry (order endpoints). run_type="manual", source="manual"
```
`client_factory(config)` returns a `TradeSmartClient` configured with timeout & connection_mode.

## 11. `src/websocket_benchmark.py`

```python
@dataclass
class WSBenchmarkConfig:
    ws_url: str; client_id: str; access_token: str
    instruments: list[str]            # ["NSE|22", ...]
    feed: str = "t"                   # "t" touchline | "d" depth
    duration_s: float = 30.0          # capped at 300
    heartbeat_s: float = 30.0
    max_reconnects: int = 2
    connect_timeout_s: float = 10.0
@dataclass
class WSBenchmarkResult:
    run_id: str; connect_ms: float | None; auth_ack_ms: float | None   # t=a -> t=ak
    subscribe_ack_ms: float | None    # t=t -> t=tk (if identifiable)
    first_message_ms: float | None    # subscribe sent -> first tf/df
    inter_arrival_ms: list[float]; message_counts: dict[str, int]
    disconnects: int; reconnects: int; errors: list[str]; started_at: str; completed_at: str
    def summary(self) -> dict          # p50/p95/p99 of inter-arrival etc. labelled as *message arrival
                                       # intervals*, NOT exchange latency
async def run_ws_benchmark(cfg: WSBenchmarkConfig, connect: Callable | None = None, stop_event=None) -> WSBenchmarkResult
    # connect injectable for tests (default websockets.connect). Uses perf_counter_ns.
def run_ws_benchmark_sync(cfg, db: Database | None = None, **kw) -> WSBenchmarkResult  # asyncio.run + persist
    # persist: create_run(run_type="websocket"), insert_ws_samples(metric in connect_ms, auth_ack_ms,
    # subscribe_ack_ms, first_message_ms, inter_arrival_ms, disconnect, reconnect), finish_run
```

## 12. `src/demo_data.py`

```python
DEMO_LABEL = "SYNTHETIC DEMO DATA"
def generate_demo_runs(db: Database, endpoints: list[Endpoint], n_runs: int = 3, iterations: int = 120,
                       seed: int = 42) -> list[str]
    # only read_only endpoints; lognormal latency per endpoint, ~1-3% api/http errors, occasional
    # timeouts; run_type="demo", is_synthetic=1, source="synthetic", notes=DEMO_LABEL; metrics saved.
def clear_demo_runs(db: Database) -> int
```

## 13. UI conventions (`src/ui/*`, `app.py`, `views/*`)

- Brand tokens (brandbook): Dark Blue `#151E34` (primary), TradeSmart Blue `#3F71EB` (accent; tints at
  80/40/10%), Green `#00B48E` (success), Red `#E02B59` (failure), Amber `#EEB268` (warning / synthetic).
  Fonts: Roboto (headings/numbers, Google Fonts) + Trebuchet MS (body/web text). Headlines ≤ 8 words,
  plain direct copy. Logo only from official artwork in `assets/` (never recreate it with a font,
  never recolour, keep clear space, min height 30px). Monogram for favicon.
- Every page: `from src.ui.theme import apply_theme` → `apply_theme(page_title)` first (sets page config,
  CSS, sidebar logo), uses `src.ui.state.get_db()/get_settings()/get_endpoints()` (st.cache_resource).
- Synthetic data is ALWAYS visibly badged (amber "SYNTHETIC DEMO" pill) and a global toggle
  "Include synthetic demo data" defaults OFF when real runs exist; never blend real + synthetic in a single
  aggregate without a banner.
- Never display tokens, secret key, app key, client id unmasked.

---

## 14. Additions made during implementation (all backwards-compatible)

- settings: `APP_VERSION`, `ENVIRONMENTS`, `DEFAULTS`; unknown `TS_ENVIRONMENT` falls back to `live` (strictest).
- redaction: `registered_secrets()`, `LOGGER_NAME`, `RedactingFilter.add_secrets()`; `redact_text` always applies globally registered secrets.
- endpoint_registry: `validate_endpoints(list)`, `Param.from_any`; top-level `rate_limits` are copied onto endpoints lacking their own.
- safety: `is_never_auto_execute(id)` (case-insensitive id/path match on place/modify/cancel/exit + explicit names incl. `ExitSNOOrder`, `PlaceMultiOrder`, `GenAcsTok`); undocumented limits use a flat 5 req/s.
- api_client: `utc_now_iso()`, `CONNECTION_MODES`, module-level `connectivity_check(...)` (DNS/TCP/TLS timings, no API call).
- benchmark_engine: `BenchmarkConfig.state_changing_test_mode`, `.user_confirmed` (ignored in live); `BenchmarkPlan.runnable_ids`; `blocked[*].missing`; `BenchmarkRunner(..., sleep=None, clock=time.monotonic)`.
- metrics: `COMPARE_METRICS`; `comparable_conditions` also reports run_type, warmup, order, synthetic-vs-measured.
- database: `utc_now_iso()`, `RESULT_COLUMNS`, `RUN_TYPES`, `RUN_STATUSES`, `list_endpoints()`, `delete_synthetic_runs()`;
  `list_runs` adds `request_count`/`endpoint_count`; `get_metrics` adds `run_started_at`, `run_type`, `run_is_synthetic`.
- exporters: `EXPORT_METRIC_COLUMNS`; spreadsheet-formula cells (`=`, `+`, `@`) are prefixed with `'`.
- demo_data: `DEFAULT_DEMO_ENDPOINTS`; `generate_demo_runs(..., warmup=2, now=None)`; demo runs use `environment="demo"`.
- websocket_benchmark: `run_ws_benchmark(..., run_id=, backoff_base_s=)`, `run_ws_benchmark_sync(..., environment="live", loop=None)`,
  `WSBenchmarkResult.connect_samples_ms`, `.duration_s`, `WSBenchmarkConfig.validate()`.
- ui: extra helper modules `src/ui/nav.py`, `data.py`, `analytics.py`, `runner.py` (background benchmark thread + stop event).
- Official paths differ from the local summary: `/GetOrderMargin` (not `/OrderMargin`), `/ExitSNOOrder` (not `/ExitSnoOrder`).
- Page scripts live in `views/` (renamed from `pages/`): Streamlit auto-discovers a `pages/` folder and, on the first
  visit to a sub-page URL after a server start, would run that page without `app.py`, losing the grouped
  `st.navigation` menu. Navigation is declared explicitly in `app.py` via `src/ui/nav.py` paths.
- `src/endpoint_check.py` + `views/8_Endpoint_Check.py`: on-demand check of one read-only endpoint
  (`run_check`, `save_check`, `preview_body`; `MAX_CHECK_REQUESTS = 20`). `TradeSmartClient.last_response_body`
  keeps the most recent body in memory for that preview only.
- `src/order_test.py` + `views/9_Order_Latency_Test.py`: opt-in LIVE order-latency test (user decision 2026-10-19:
  live guarded, 10 orders/IST day, auto-cancel). The only path that sends PlaceOrder/CancelOrder; guardrails are
  enforced in `run_order_test` before anything is sent. `src/safety.py` still blocks order endpoints everywhere else.
- `pytest.ini` restricts collection to `tests/` (page files ending in `_Test.py` must never be imported by pytest).
- `src/script_master.py` + `src/ui/instrument_picker.py`: public script-master download (URLs from `endpoints.json`
  `script_master`; no credentials) into `<TS_DB_PATH folder>/scriptmaster/instruments.db` (tables `instruments`, `meta`).
  API: `Instrument` (frozen; `label`, `key`, `is_derivative`), `refresh(exchanges=None, *, force=False, client=None, now=None)`
  (once per IST day per exchange, atomic per-exchange replace, zip-size guards, no retries), `start_background_refresh()`
  (idempotent daemon thread), `status()`, `search(query, exchanges=None, limit=25)`, `lookup(exch, tsym)`,
  `by_token(exch, token)`, `has_data()`, `set_storage_dir()`. Started from `app.py` via `state.ensure_script_master()`
  (cache_resource, ttl 1h; `TS_SCRIPT_MASTER_AUTOSTART=0` disables). UI: `instrument_picker(key, *, exchanges, label,
  help, default_tsym) -> Instrument | None`, `instrument_fields(...)` (picker + manual override; fills exch/token/tsym
  and `sym` = `EXCH:TSYM`), `sidebar_status()`; Settings has a *Script master* tab.
- 2026-10-22: order test raised to 100 orders/IST day; bursts of <= 8 (`MAX_BURST_SIZE`) with >= 1 s pause; `OrderRateGate` caps PlaceOrder+CancelOrder at 8 per rolling 1.25 s (`ORDER_RATE_WINDOW_S`, TradeSmart limit 10 OPS); one quote per burst; `stop_event` support; the page runs batches in a background thread with a Stop button.
- 2026-10-26: order test = one input for the whole batch (<= 1000/IST day, `MAX_ORDERS_PER_DAY`). Each burst places up to 8 orders back to back, then cancels all of them (every accepted order is cancelled even if a cancel fails or the batch stops), confirms status, and waits `burst_pause_s` (1-60 s, default 3) from the burst's last order. Results are written per burst phase and always flushed in `finally`.
