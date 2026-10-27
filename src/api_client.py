"""HTTPX client for the TradeSmart REST API with per-request latency measurement.

Every REST call is ``POST {base_url}/<Endpoint>`` with body ``jData=<json>`` and (for authenticated
endpoints) ``Authorization: Bearer <access_token>``. HTTP 200 alone is not success: the JSON ``stat`` field
must be ``Ok`` (see :func:`classify_response`).

Timing uses ``time.perf_counter_ns()`` taken immediately before the request is sent and after the full
response body has been read. In ``cold`` mode a fresh ``httpx.Client`` is created (outside the timed
window) and closed for every request, so each sample includes DNS + TCP + TLS setup. In ``warm`` mode one
persistent keep-alive client is reused. There are no automatic retries; network failures are returned as
failed :class:`RequestResult` rows with the real elapsed time, never raised or discarded.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import ssl
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable
from urllib.parse import urlparse

import httpx

from src.redaction import redact_text

if TYPE_CHECKING:
    from src.endpoint_registry import Endpoint

logger = logging.getLogger("tsbench")

OUTCOMES = ("success", "api_error", "http_error", "rate_limited", "timeout", "connect_error",
            "dns_error", "other_error")
SERVER_TIMING_HEADERS = ("server-timing", "x-response-time", "x-runtime", "x-processing-time")
CONNECTION_MODES = ("warm", "cold")

_NO_DATA_RE = re.compile(r"no\s*data", re.IGNORECASE)
_DNS_HINTS = ("getaddrinfo", "name or service not known", "nodename", "name resolution",
              "no address associated", "temporary failure in name resolution", "11001")


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with microseconds and a trailing ``Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def encode_jdata(payload: dict) -> str:
    """Encode a payload the way the API expects: ``jData=`` + compact JSON."""
    return "jData=" + json.dumps(payload, separators=(",", ":"))


def classify_response(status: int, body: bytes) -> tuple[bool, str, str | None, str | None]:
    """Classify a response into ``(success, outcome, api_stat, emsg)``.

    * 429 -> ``rate_limited``; other non-2xx -> ``http_error``.
    * 2xx + ``stat == "Ok"`` or a JSON list -> ``success``.
    * 2xx + ``Not_Ok`` whose ``emsg`` matches /no data/i -> ``success`` (completed request, empty dataset).
    * 2xx + other ``Not_Ok`` or missing ``stat`` -> ``api_error``; invalid JSON -> ``api_error`` "invalid JSON".
    """
    parsed: Any = None
    valid_json = True
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace")) if body else None
        if parsed is None:
            valid_json = False
    except (ValueError, UnicodeDecodeError):
        valid_json = False

    api_stat: str | None = None
    emsg: str | None = None
    if isinstance(parsed, dict):
        stat_value = parsed.get("stat")
        api_stat = str(stat_value) if stat_value is not None else None
        if parsed.get("emsg") is not None:
            emsg = str(parsed.get("emsg"))

    if status == 429:
        return False, "rate_limited", api_stat, emsg
    if not 200 <= status < 300:
        return False, "http_error", api_stat, emsg
    if not valid_json:
        return False, "api_error", None, "invalid JSON"
    if isinstance(parsed, list):
        return True, "success", None, None
    if isinstance(parsed, dict):
        if api_stat is not None and api_stat.lower() == "ok":
            return True, "success", api_stat, emsg
        if api_stat is not None and api_stat.lower() == "not_ok" and emsg and _NO_DATA_RE.search(emsg):
            return True, "success", api_stat, emsg
        if api_stat is None:
            return False, "api_error", None, emsg or "response has no 'stat' field"
        return False, "api_error", api_stat, emsg
    return False, "api_error", None, "unexpected JSON type"


@dataclass
class RequestResult:
    """One timed request (or a manual/synthetic entry). Error messages are always redacted."""

    run_id: str
    endpoint_id: str
    endpoint_name: str
    path: str
    method: str
    seq: int
    is_warmup: bool
    connection_mode: str
    started_at: str
    completed_at: str
    latency_ms: float | None
    http_status: int | None
    success: bool
    outcome: str
    is_timeout: bool
    error_type: str | None
    error_message: str | None
    response_bytes: int
    api_stat: str | None
    server_timing: str | None
    source: str = "measured"

    def to_row(self) -> dict:
        """Flat dict matching the ``request_results`` table columns."""
        return asdict(self)


def _server_timing(headers: httpx.Headers) -> str | None:
    parts = [f"{name}: {headers[name]}" for name in SERVER_TIMING_HEADERS if name in headers]
    return "; ".join(parts) if parts else None


def _is_dns_error(exc: BaseException) -> bool:
    text = f"{exc!r} {exc} {exc.__cause__!r}".lower()
    return any(hint in text for hint in _DNS_HINTS) or isinstance(exc.__cause__, socket.gaierror)


def _shared_ssl_context() -> ssl.SSLContext:
    """One verified SSL context reused by cold clients (loading CA certs is client-side, not network)."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        try:
            import certifi

            _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
        except ImportError:  # pragma: no cover - certifi ships with httpx
            _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


_SSL_CONTEXT: ssl.SSLContext | None = None


# --------------------------------------------------------------------------------------------------
# Connectivity check (no API request, no credentials)
# --------------------------------------------------------------------------------------------------
def connectivity_check(
    base_url: str,
    timeout_s: float = 5.0,
    *,
    resolver: Callable[..., Any] | None = None,
    connector: Callable[..., Any] | None = None,
    tls_wrapper: Callable[[Any, str], Any] | None = None,
) -> dict:
    """Time DNS resolution, TCP connect and TLS handshake to the base URL host.

    Sends no HTTP request and no credentials. ``resolver`` (getaddrinfo-like), ``connector``
    (create_connection-like) and ``tls_wrapper(sock, host) -> ssl_sock`` are injectable for tests.
    Returns ``{ok, host, port, ip, dns_ms, connect_ms, tls_ms, total_ms, error}``.
    """
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    scheme = (parsed.scheme or "https").lower()
    port = parsed.port or (443 if scheme in ("https", "wss") else 80)
    result: dict[str, Any] = {"ok": False, "host": host, "port": port, "ip": None, "dns_ms": None,
                              "connect_ms": None, "tls_ms": None, "total_ms": None, "error": None}
    if not host:
        result["error"] = "base URL has no host"
        return result
    resolve = resolver or socket.getaddrinfo
    connect = connector or socket.create_connection
    if tls_wrapper is None:

        def tls_wrapper(sock: Any, server_hostname: str) -> Any:
            return _shared_ssl_context().wrap_socket(sock, server_hostname=server_hostname)

    t_start = time.perf_counter_ns()
    stage = "dns"
    sock: Any = None
    try:
        t0 = time.perf_counter_ns()
        infos = resolve(host, port, 0, socket.SOCK_STREAM)
        result["dns_ms"] = (time.perf_counter_ns() - t0) / 1e6
        if not infos:
            raise OSError("DNS returned no addresses")
        ip = infos[0][4][0]
        result["ip"] = ip
        stage = "connect"
        t0 = time.perf_counter_ns()
        sock = connect((ip, port), timeout_s)
        result["connect_ms"] = (time.perf_counter_ns() - t0) / 1e6
        if scheme in ("https", "wss"):
            stage = "tls"
            t0 = time.perf_counter_ns()
            sock = tls_wrapper(sock, host)
            result["tls_ms"] = (time.perf_counter_ns() - t0) / 1e6
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - report any failure as data
        result["error"] = redact_text(f"{stage} failed: {type(exc).__name__}: {exc}")
    finally:
        result["total_ms"] = (time.perf_counter_ns() - t_start) / 1e6
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
    return result


# --------------------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------------------
class TradeSmartClient:
    """Timed TradeSmart REST client. Use as a context manager or call :meth:`close`."""

    def __init__(
        self,
        base_url: str,
        access_token: str = "",
        timeout_s: float = 10.0,
        connection_mode: str = "warm",
        transport: httpx.BaseTransport | None = None,
        secrets: Iterable[str] = (),
    ) -> None:
        if connection_mode not in CONNECTION_MODES:
            raise ValueError(f"connection_mode must be one of {CONNECTION_MODES}")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self.connection_mode = connection_mode
        self._access_token = access_token or ""
        self._transport = transport
        self._secrets = [s for s in [*secrets, self._access_token] if s]
        self._client: httpx.Client | None = self._new_client() if connection_mode == "warm" else None
        # Body of the most recent response, kept in memory only for interactive previews (the Endpoint
        # check page). Never stored, logged or exported; overwritten by the next request.
        self.last_response_body: bytes | None = None

    def __repr__(self) -> str:  # never expose the token
        return f"TradeSmartClient(base_url={self.base_url!r}, mode={self.connection_mode!r})"

    def _new_client(self) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": httpx.Timeout(self.timeout_s), "follow_redirects": False}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        else:
            kwargs["verify"] = _shared_ssl_context()
        return httpx.Client(**kwargs)

    def _redact(self, text: str | None) -> str | None:
        return None if text is None else redact_text(text, self._secrets)

    def timed_post(self, ep: "Endpoint", payload: dict, *, run_id: str, seq: int, is_warmup: bool) -> RequestResult:
        """Send one POST and return its timed :class:`RequestResult`. Never raises for request errors."""
        self.last_response_body = None
        url = f"{self.base_url}{ep.url_path}"
        headers: dict[str, str] = {}
        if ep.auth_required and self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"

        def make(**kw: Any) -> RequestResult:
            base: dict[str, Any] = dict(
                run_id=run_id, endpoint_id=ep.id, endpoint_name=ep.name, path=ep.url_path, method=ep.method,
                seq=seq, is_warmup=is_warmup, connection_mode=self.connection_mode, started_at="",
                completed_at="", latency_ms=None, http_status=None, success=False, outcome="other_error",
                is_timeout=False, error_type=None, error_message=None, response_bytes=0, api_stat=None,
                server_timing=None,
            )
            base.update(kw)
            return RequestResult(**base)

        client: httpx.Client | None = None
        owns_client = False
        try:
            if self._client is not None:
                client = self._client
            else:
                client = self._new_client()
                owns_client = True
            request = client.build_request(ep.method, url, content=encode_jdata(payload).encode("utf-8"),
                                           headers=headers)
        except Exception as exc:  # noqa: BLE001 - request never sent
            if owns_client and client is not None:
                client.close()
            now = utc_now_iso()
            return make(started_at=now, completed_at=now, error_type=type(exc).__name__,
                        error_message=self._redact(f"request not sent: {exc}"))

        started_at = utc_now_iso()
        t0 = time.perf_counter_ns()
        try:
            response = client.send(request)  # non-streaming: returns after the full body is read
            t1 = time.perf_counter_ns()
            completed_at = utc_now_iso()
        except httpx.TimeoutException as exc:
            t1 = time.perf_counter_ns()
            return make(started_at=started_at, completed_at=utc_now_iso(), latency_ms=(t1 - t0) / 1e6,
                        outcome="timeout", is_timeout=True, error_type=type(exc).__name__,
                        error_message=self._redact(f"timeout after {self.timeout_s:g}s: {exc}"))
        except httpx.ConnectError as exc:
            t1 = time.perf_counter_ns()
            outcome = "dns_error" if _is_dns_error(exc) else "connect_error"
            return make(started_at=started_at, completed_at=utc_now_iso(), latency_ms=(t1 - t0) / 1e6,
                        outcome=outcome, error_type=type(exc).__name__, error_message=self._redact(str(exc)))
        except Exception as exc:  # noqa: BLE001 - any other transport/protocol failure
            t1 = time.perf_counter_ns()
            return make(started_at=started_at, completed_at=utc_now_iso(), latency_ms=(t1 - t0) / 1e6,
                        outcome="other_error", error_type=type(exc).__name__,
                        error_message=self._redact(str(exc) or type(exc).__name__))
        finally:
            if owns_client and client is not None:
                client.close()

        body = response.content
        self.last_response_body = body
        success, outcome, api_stat, emsg = classify_response(response.status_code, body)
        error_type: str | None = None
        error_message: str | None = None
        if not success:
            error_type = f"HTTP {response.status_code}" if outcome in ("http_error", "rate_limited") else "api_error"
            error_message = self._redact(emsg or response.reason_phrase or outcome)
        return make(started_at=started_at, completed_at=completed_at, latency_ms=(t1 - t0) / 1e6,
                    http_status=response.status_code, success=success, outcome=outcome,
                    error_type=error_type, error_message=error_message, response_bytes=len(body),
                    api_stat=api_stat, server_timing=_server_timing(response.headers))

    def connectivity_check(self, **kwargs: Any) -> dict:
        """DNS + TCP + TLS handshake timing to the base host (no API call). See :func:`connectivity_check`."""
        kwargs.setdefault("timeout_s", self.timeout_s)
        return connectivity_check(self.base_url, **kwargs)

    def close(self) -> None:
        """Close the persistent (warm) client, if any."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "TradeSmartClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
