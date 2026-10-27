"""Tests for src.auth (GenAcsTok via httpx.MockTransport; never hits the network)."""
from __future__ import annotations

import hashlib
import json
import logging
from types import SimpleNamespace
from urllib.parse import unquote_plus

import httpx
import pytest

from src.auth import build_authorize_url, compute_checksum, extract_auth_code, generate_access_token

APP_KEY, SECRET_KEY, CODE = "appKEY123", "s3cretKEY456", "authCODE789"


def settings(**kw):
    base = dict(base_url="https://mock.tradesmart.test/NorenWClientAPIv2/", app_key=APP_KEY, secret_key=SECRET_KEY)
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_authorize_url():
    assert build_authorize_url("https://x.test/oauth", "KEY1") == "https://x.test/oauth?client_id=KEY1"
    assert build_authorize_url("https://x.test/oauth?a=1", "KEY1") == "https://x.test/oauth?a=1&client_id=KEY1"
    with pytest.raises(ValueError):
        build_authorize_url("https://x.test/oauth", "")


def test_compute_checksum():
    expected = hashlib.sha256((APP_KEY + SECRET_KEY + CODE).encode()).hexdigest()
    assert compute_checksum(APP_KEY, SECRET_KEY, CODE) == expected


@pytest.mark.parametrize("value", [
    "https://my.app/callback?code=authCODE789",
    "https://my.app/callback?state=1&code=authCODE789",
    "  authCODE789  ",
    "code=authCODE789",
])
def test_extract_auth_code(value):
    assert extract_auth_code(value) == CODE


@pytest.mark.parametrize("value,expected", [
    # a literal '+' must survive (parse_qs would turn it into a space and break the checksum)
    ("http://49.248.77.42/callback?code=ab+cd/ef==", "ab+cd/ef=="),
    ("http://49.248.77.42/callback?code=ab%2Bcd%2Fef%3D%3D&state=x", "ab+cd/ef=="),
    ("ab%2Bcd%3D%3D", "ab+cd=="),                      # bare code copied still percent-encoded
    ("ab+cd==", "ab+cd=="),
    ("'authCODE789'", CODE),                            # stray quotes from copy/paste
    ("https://my.app/callback?authCode=authCODE789", CODE),
    ("https://my.app/callback#code=authCODE789", CODE),
])
def test_extract_auth_code_preserves_exact_code(value, expected):
    assert extract_auth_code(value) == expected


def test_checksum_uses_exact_code_with_plus():
    code = extract_auth_code("http://x/callback?code=ab+cd==")
    assert compute_checksum(APP_KEY, SECRET_KEY, code) == hashlib.sha256(
        (APP_KEY + SECRET_KEY + "ab+cd==").encode()).hexdigest()


@pytest.mark.parametrize("value", ["", "https://my.app/callback?state=1", "two words"])
def test_extract_auth_code_invalid(value):
    with pytest.raises(ValueError):
        extract_auth_code(value)


def test_generate_access_token_success(caplog):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"stat": "Ok", "access_token": "TOKENxyz987"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger="tsbench"):
        res = generate_access_token(settings(), "https://my.app/cb?code=" + CODE, client=client)
    assert res.ok and res.access_token == "TOKENxyz987" and res.error is None
    assert res.raw_keys == ["access_token", "stat"]
    assert seen["url"] == "https://mock.tradesmart.test/NorenWClientAPIv2/GenAcsTok"
    assert seen["auth"] is None  # GenAcsTok must not carry a Bearer token
    assert seen["body"].startswith("jData=")
    payload = json.loads(unquote_plus(seen["body"][len("jData="):]))
    assert payload == {"code": CODE, "checksum": compute_checksum(APP_KEY, SECRET_KEY, CODE)}
    for secret in (SECRET_KEY, CODE, "TOKENxyz987", APP_KEY, payload["checksum"]):
        assert secret not in caplog.text
    assert "TOKENxyz987" not in repr(res)


def test_generate_access_token_api_error_redacted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"stat": "Not_Ok", "emsg": f"Invalid checksum for code {CODE}"})

    res = generate_access_token(settings(), CODE, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert not res.ok and res.access_token is None
    assert "Invalid checksum" in res.error and CODE not in res.error


def test_generate_access_token_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect, body={request.content.decode()}")

    res = generate_access_token(settings(), CODE, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert not res.ok and "ConnectError" in res.error
    assert CODE not in res.error and SECRET_KEY not in res.error


def test_generate_access_token_missing_keys():
    res = generate_access_token(settings(secret_key=""), CODE)
    assert not res.ok and "TS_SECRET_KEY" in res.error
    res2 = generate_access_token(settings(), "https://my.app/cb?state=1")
    assert not res2.ok and "code" in res2.error
