"""Tests for src.outbound: pinning outgoing connections to one local address."""
from __future__ import annotations

import httpx
import pytest

from src import outbound
from src.api_client import TradeSmartClient
from src.endpoint_registry import Endpoint

ADDRESS = "192.0.2.5"
EP = Endpoint(id="GetQuotes", name="Get Quotes", category="Scrips", path="/GetQuotes")


@pytest.fixture(autouse=True)
def _reset():
    outbound.set_local_address(None)
    yield
    outbound.set_local_address(None)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_address_means_the_os_decides(value):
    outbound.set_local_address(ADDRESS)
    outbound.set_local_address(value)
    assert outbound.local_address() is None


def test_address_is_stripped():
    outbound.set_local_address(f" {ADDRESS} ")
    assert outbound.local_address() == ADDRESS


def test_kwargs_without_an_address():
    assert outbound.httpx_transport_kwargs() == {"verify": True}
    assert outbound.httpx_transport_kwargs("ctx") == {"verify": "ctx"}
    assert outbound.websocket_kwargs() == {}


def test_kwargs_with_an_address():
    outbound.set_local_address(ADDRESS)
    kwargs = outbound.httpx_transport_kwargs()
    assert set(kwargs) == {"transport"} and isinstance(kwargs["transport"], httpx.HTTPTransport)
    assert kwargs["transport"]._pool._local_address == ADDRESS
    assert outbound.websocket_kwargs() == {"local_addr": (ADDRESS, 0)}


def test_kwargs_are_accepted_by_httpx_client():
    with httpx.Client(**outbound.httpx_transport_kwargs()):
        pass
    outbound.set_local_address(ADDRESS)
    with httpx.Client(**outbound.httpx_transport_kwargs()):
        pass


def test_client_without_an_address_is_not_bound():
    with TradeSmartClient("https://x.test") as client:
        assert client._client._transport._pool._local_address is None


@pytest.mark.parametrize("mode", ["warm", "cold"])
def test_client_with_an_address_is_bound(mode):
    outbound.set_local_address(ADDRESS)
    with TradeSmartClient("https://x.test", connection_mode=mode) as client:
        fresh = client._new_client()
        try:
            assert fresh._transport._pool._local_address == ADDRESS
        finally:
            fresh.close()


def test_client_keeps_its_tls_context_when_bound():
    from src.api_client import _shared_ssl_context

    outbound.set_local_address(ADDRESS)
    with TradeSmartClient("https://x.test") as client:
        assert client._client._transport._pool._ssl_context is _shared_ssl_context()


def test_injected_transport_wins_and_requests_still_work():
    outbound.set_local_address(ADDRESS)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"stat": "Ok"}))
    for mode in ("warm", "cold"):
        with TradeSmartClient("https://x.test", transport=transport, connection_mode=mode) as client:
            assert client.timed_post(EP, {"uid": "x"}, run_id="r", seq=1, is_warmup=False).success
