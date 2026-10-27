"""Tests for src.api_client using httpx.MockTransport and injected socket fakes (no network)."""
from __future__ import annotations

import json
import re
import socket
from datetime import datetime

import httpx
import pytest

from src.api_client import (
    OUTCOMES,
    RequestResult,
    TradeSmartClient,
    classify_response,
    connectivity_check,
    encode_jdata,
)
from src.endpoint_registry import Endpoint

BASE = "https://mock.tradesmart.test/NorenWClientAPIv2"
TOKEN = "SuperSecretToken123"
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

EP = Endpoint(id="UserDetails", name="User Details", category="Authentication", path="/UserDetails",
              request_body_template={"uid": "{{ClientId}}"})


def make_client(handler, **kw) -> TradeSmartClient:
    return TradeSmartClient(BASE, access_token=TOKEN, transport=httpx.MockTransport(handler), **kw)


def call(client: TradeSmartClient, ep: Endpoint = EP, payload: dict | None = None, **kw) -> RequestResult:
    return client.timed_post(ep, payload or {"uid": "AB1234"}, run_id="run-1", seq=kw.get("seq", 1),
                             is_warmup=kw.get("is_warmup", False))


def test_encode_jdata():
    assert encode_jdata({"uid": "A", "n": 1}) == 'jData={"uid":"A","n":1}'


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (200, b'{"stat":"Ok","x":1}', (True, "success", "Ok", None)),
        (200, b'[{"a":1}]', (True, "success", None, None)),
        (200, b'{"stat":"Not_Ok","emsg":"Error Occurred : 5 \\"no data\\""}', (True, "success", "Not_Ok", None)),
        (200, b'{"stat":"Not_Ok","emsg":"Session Expired"}', (False, "api_error", "Not_Ok", "Session Expired")),
        (200, b"<html>oops", (False, "api_error", None, "invalid JSON")),
        (200, b"", (False, "api_error", None, "invalid JSON")),
        (429, b'{"stat":"Not_Ok","emsg":"Too many"}', (False, "rate_limited", "Not_Ok", "Too many")),
        (500, b"Internal", (False, "http_error", None, None)),
        (401, b'{"stat":"Not_Ok","emsg":"Unauthorized"}', (False, "http_error", "Not_Ok", "Unauthorized")),
    ],
)
def test_classify_response(status, body, expected):
    result = classify_response(status, body)
    assert result[:3] == expected[:3]
    if expected[3] is not None:
        assert result[3] == expected[3]
    assert result[1] in OUTCOMES


def test_timed_post_success_and_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"stat": "Ok", "uname": "X"},
                              headers={"Server-Timing": "app;dur=12.5", "X-Response-Time": "13ms"})

    with make_client(handler) as client:
        r = call(client, seq=3, is_warmup=True)
    assert seen == {"method": "POST", "url": f"{BASE}/UserDetails", "auth": f"Bearer {TOKEN}",
                    "body": 'jData={"uid":"AB1234"}'}
    assert r.success and r.outcome == "success" and r.http_status == 200 and r.api_stat == "Ok"
    assert r.seq == 3 and r.is_warmup and r.connection_mode == "warm" and r.source == "measured"
    assert r.latency_ms is not None and r.latency_ms >= 0
    assert ISO_Z.match(r.started_at) and ISO_Z.match(r.completed_at)
    assert datetime.fromisoformat(r.completed_at[:-1]) >= datetime.fromisoformat(r.started_at[:-1])
    assert r.response_bytes > 0
    assert "app;dur=12.5" in r.server_timing and "x-response-time: 13ms" in r.server_timing
    row = r.to_row()
    assert row["endpoint_id"] == "UserDetails" and row["path"] == "/UserDetails"


def test_no_bearer_when_auth_not_required():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"stat": "Ok"})

    ep = Endpoint(id="GenAcsTok", name="t", category="a", path="/GenAcsTok", auth_required=False)
    with make_client(handler) as client:
        call(client, ep)
    assert seen["auth"] is None


def test_api_error_message_redacted():
    def handler(request):
        return httpx.Response(200, json={"stat": "Not_Ok", "emsg": f"Invalid token {TOKEN} for uid AB1234"})

    with make_client(handler) as client:
        r = call(client)
    assert not r.success and r.outcome == "api_error" and r.api_stat == "Not_Ok"
    assert TOKEN not in (r.error_message or "") and "Invalid" in r.error_message


def test_rate_limited_and_http_error():
    responses = iter([httpx.Response(429, text="slow down"), httpx.Response(503, text="unavailable")])
    with make_client(lambda req: next(responses)) as client:
        r1, r2 = call(client), call(client)
    assert r1.outcome == "rate_limited" and r1.http_status == 429 and not r1.success
    assert r2.outcome == "http_error" and r2.http_status == 503 and r2.error_type == "HTTP 503"


@pytest.mark.parametrize(
    "exc, outcome, is_timeout",
    [
        (httpx.ReadTimeout("timed out"), "timeout", True),
        (httpx.ConnectTimeout("connect timed out"), "timeout", True),
        (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), "dns_error", False),
        (httpx.ConnectError("[Errno -2] Name or service not known"), "dns_error", False),
        (httpx.ConnectError("[WinError 10061] connection refused"), "connect_error", False),
        (httpx.RemoteProtocolError("server disconnected"), "other_error", False),
    ],
)
def test_network_errors_recorded_not_raised(exc, outcome, is_timeout):
    def handler(request):
        raise exc

    with make_client(handler) as client:
        r = call(client)
    assert r.outcome == outcome and r.is_timeout is is_timeout and not r.success
    assert r.latency_ms is not None and r.latency_ms >= 0  # real elapsed time recorded
    assert r.error_type == type(exc).__name__
    assert TOKEN not in (r.error_message or "")


def test_error_message_never_contains_token_or_payload():
    def handler(request):
        raise httpx.ConnectError(f"failed sending Authorization: Bearer {TOKEN} body {request.content!r}")

    with make_client(handler) as client:
        r = call(client, payload={"uid": "AB1234", "accesstoken": "ZZsecretZZ"})
    assert TOKEN not in r.error_message and "ZZsecretZZ" not in r.error_message


def test_no_automatic_retries():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("refused")

    with make_client(handler) as client:
        call(client)
    assert len(calls) == 1


def test_cold_mode_creates_fresh_client_per_request(monkeypatch):
    created = []
    original_init = httpx.Client.__init__

    def tracking_init(self, *a, **kw):
        created.append(self)
        original_init(self, *a, **kw)

    monkeypatch.setattr(httpx.Client, "__init__", tracking_init)
    handler = lambda req: httpx.Response(200, json={"stat": "Ok"})  # noqa: E731
    cold = make_client(handler, connection_mode="cold")
    assert created == []  # no persistent client in cold mode
    r1, r2 = call(cold), call(cold)
    assert len(created) == 2 and all(c.is_closed for c in created)
    assert r1.connection_mode == "cold" and r2.success
    warm = make_client(handler, connection_mode="warm")
    call(warm), call(warm)
    assert len(created) == 3  # one persistent client reused
    warm.close()
    assert created[-1].is_closed


def test_invalid_connection_mode():
    with pytest.raises(ValueError):
        TradeSmartClient(BASE, connection_mode="hot")


def test_repr_hides_token():
    client = make_client(lambda r: httpx.Response(200, json={"stat": "Ok"}))
    assert TOKEN not in repr(client)
    client.close()


# --------------------------------------------------------------------------------------------------
# connectivity check (all socket operations injected)
# --------------------------------------------------------------------------------------------------
class FakeSock:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_connectivity_check_success():
    calls = {}
    sock = FakeSock()

    def resolver(host, port, family, socktype):
        calls["resolve"] = (host, port)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.5", port))]

    def connector(addr, timeout):
        calls["connect"] = (addr, timeout)
        return sock

    def tls(s, host):
        calls["tls"] = host
        return s

    client = TradeSmartClient(BASE, access_token=TOKEN, timeout_s=3.0,
                              transport=httpx.MockTransport(lambda r: pytest.fail("no HTTP request allowed")))
    res = client.connectivity_check(resolver=resolver, connector=connector, tls_wrapper=tls)
    client.close()
    assert res["ok"] and res["error"] is None and res["ip"] == "203.0.113.5"
    assert calls == {"resolve": ("mock.tradesmart.test", 443), "connect": (("203.0.113.5", 443), 3.0),
                     "tls": "mock.tradesmart.test"}
    assert all(res[k] is not None and res[k] >= 0 for k in ("dns_ms", "connect_ms", "tls_ms", "total_ms"))
    assert sock.closed


def test_connectivity_check_dns_failure():
    def resolver(*a):
        raise socket.gaierror(11001, "getaddrinfo failed")

    res = connectivity_check(BASE, resolver=resolver, connector=lambda *a: pytest.fail("no connect"))
    assert not res["ok"] and res["error"].startswith("dns failed") and res["connect_ms"] is None


def test_connectivity_check_tls_failure_closes_socket():
    sock = FakeSock()

    def bad_tls(s, host):
        raise OSError("handshake failure")

    res = connectivity_check(BASE, resolver=lambda *a: [(2, 1, 6, "", ("198.51.100.1", 443))],
                             connector=lambda addr, t: sock, tls_wrapper=bad_tls)
    assert not res["ok"] and "tls failed" in res["error"] and res["connect_ms"] is not None
    assert sock.closed


def test_result_rows_are_json_serialisable():
    with make_client(lambda r: httpx.Response(200, json={"stat": "Ok"})) as client:
        json.dumps(call(client).to_row())


def test_last_response_body_kept_in_memory_and_reset():
    bodies = iter([b'{"stat":"Ok","x":1}'])

    def handler(request):
        try:
            return httpx.Response(200, content=next(bodies))
        except StopIteration:
            raise httpx.ConnectError("refused")

    with make_client(handler) as client:
        assert client.last_response_body is None
        call(client)
        assert client.last_response_body == b'{"stat":"Ok","x":1}'
        call(client)  # failed request must not leave the previous body behind
        assert client.last_response_body is None
