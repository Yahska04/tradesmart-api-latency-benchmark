"""Tests for src.network_guard: orders only leave through the registered static IP, and the guard fails closed."""
from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from src import network_guard as ng
from src.database import Database
from src.settings import Settings

IP = "203.0.113.10"
OTHER = "198.51.100.7"
URL = "https://ip.test/"


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


@pytest.fixture
def settings(tmp_path):
    return Settings(base_url="https://x.test", ws_url="wss://x.test", oauth_url="https://x.test/o", app_key="", secret_key="",
                    client_id="", redirect_url="", access_token="", environment="mock", static_ip="",
                    db_path=tmp_path / "t.db", log_path=tmp_path / "a.log", ip_check_url=URL, enforce_static_ip=True)


def echo(body: str = IP, status: int = 200) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, text=body)))


def timing_out() -> httpx.Client:
    def handler(request):
        raise httpx.ConnectTimeout("timed out")
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------- parsing
@pytest.mark.parametrize("text,expected", [
    (IP, IP), (f"  {IP}\n", IP), ("2001:DB8:0:0::1", "2001:db8::1"),
])
def test_normalize_ip(text, expected):
    assert ng.normalize_ip(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "not-an-ip", "1.2.3", "1.2.3.4.5", "256.1.1.1", None, "<html>"])
def test_normalize_ip_rejects_garbage(text):
    with pytest.raises(ValueError):
        ng.normalize_ip(text)


@pytest.mark.parametrize("text,expected", [
    ("8.8.8.8", True), ("10.0.0.1", False), ("192.168.1.1", False), ("127.0.0.1", False), ("169.254.1.1", False),
    ("garbage", False), ("", False),
])
def test_is_public_ip(text, expected):
    assert ng.is_public_ip(text) is expected


# ---------------------------------------------------------------------------- expected IP
def test_effective_static_ip_priority(settings, db):
    assert ng.effective_static_ip(settings, db) == ""
    env = replace(settings, static_ip=OTHER)
    assert ng.effective_static_ip(env) == OTHER                 # no database
    assert ng.effective_static_ip(env, db) == OTHER             # nothing saved yet
    ng.save_static_ip(db, IP, changed_by="admin")
    assert ng.effective_static_ip(env, db) == IP                # saved value wins


def test_effective_static_ip_falls_back_when_the_database_fails(settings):
    class Broken:
        def get_setting(self, key):
            raise OSError("disk gone")
    assert ng.effective_static_ip(replace(settings, static_ip=OTHER), Broken()) == OTHER


def test_save_static_ip_normalizes_and_persists(tmp_path, db):
    assert ng.save_static_ip(db, f" {IP}\n", changed_by="admin") == IP
    assert Database(tmp_path / "t.db").get_setting(ng.STATIC_IP_SETTING) == IP   # survives a new connection


def test_save_static_ip_rejects_invalid_and_keeps_the_old_value(db):
    ng.save_static_ip(db, IP, changed_by="admin")
    with pytest.raises(ValueError):
        ng.save_static_ip(db, "not-an-ip", changed_by="admin")
    assert db.get_setting(ng.STATIC_IP_SETTING) == IP


@pytest.mark.parametrize("value", ["", "   "])
def test_save_static_ip_clearing_falls_back_to_env(settings, db, value):
    ng.save_static_ip(db, IP, changed_by="admin")
    assert ng.save_static_ip(db, value, changed_by="admin") == ""
    assert ng.effective_static_ip(replace(settings, static_ip=OTHER), db) == OTHER


def test_set_setting_accepts_the_key(db):
    db.set_setting(ng.STATIC_IP_SETTING, IP)                    # must not trip the secret-looking-key check
    assert db.get_setting(ng.STATIC_IP_SETTING) == IP


# ---------------------------------------------------------------------------- detection
def test_detect_public_ip_good():
    assert ng.detect_public_ip(URL, client=echo(f"{IP}\n")) == IP


def test_detect_public_ip_garbage_body():
    with pytest.raises(ValueError):
        ng.detect_public_ip(URL, client=echo("<html>blocked</html>"))


def test_detect_public_ip_http_500():
    with pytest.raises(httpx.HTTPStatusError):
        ng.detect_public_ip(URL, client=echo(IP, status=500))


def test_detect_public_ip_timeout():
    with pytest.raises(httpx.TimeoutException):
        ng.detect_public_ip(URL, client=timing_out())


def test_detect_leaves_a_supplied_client_open():
    client = echo()
    ng.detect_public_ip(URL, client=client)
    assert not client.is_closed


# ---------------------------------------------------------------------------- egress check
def test_match(settings):
    check = ng.check_egress(replace(settings, static_ip=IP), client=echo())
    assert check.matches and check.problem is None and check.order_block_reason is None


def test_match_compares_canonical_forms(settings):
    check = ng.check_egress(replace(settings, static_ip="2001:DB8::1"), client=echo("2001:db8:0::1"))
    assert check.matches


def test_mismatch_blocks_orders_when_enforced(settings):
    check = ng.check_egress(replace(settings, static_ip=OTHER), client=echo())
    assert not check.matches and IP in check.problem and OTHER in check.problem
    assert check.order_block_reason == check.problem


def test_not_set_blocks_orders_when_enforced(settings):
    check = ng.check_egress(settings, client=echo())
    assert not check.matches and "not been set" in check.problem and check.order_block_reason


@pytest.mark.parametrize("client", [
    lambda: echo("garbage"), lambda: echo(IP, status=500), timing_out,
], ids=["garbage", "http500", "timeout"])
def test_cant_detect_fails_closed(settings, client):
    check = ng.check_egress(replace(settings, static_ip=IP), client=client())
    assert check.detected is None and check.error and not check.matches
    assert "Couldn't detect" in check.problem and check.order_block_reason


def test_invalid_expected_ip_fails_closed(settings):
    check = ng.check_egress(replace(settings, static_ip="nonsense"), client=echo())
    assert not check.matches and check.order_block_reason


@pytest.mark.parametrize("kw,client", [
    (dict(static_ip=OTHER), lambda: echo()),
    (dict(static_ip=""), lambda: echo()),
    (dict(static_ip=IP), timing_out),
], ids=["mismatch", "not-set", "cant-detect"])
def test_problem_is_only_a_warning_when_not_enforced(settings, kw, client):
    check = ng.check_egress(replace(settings, enforce_static_ip=False, **kw), client=client())
    assert check.problem and check.order_block_reason is None


def test_saved_ip_is_used_by_the_check(settings, db):
    ng.save_static_ip(db, IP, changed_by="admin")
    assert ng.check_egress(replace(settings, static_ip=OTHER), db, client=echo()).matches


def test_order_guard_does_a_fresh_check_each_call(settings, monkeypatch):
    replies = iter([IP, OTHER])
    monkeypatch.setattr(ng, "detect_public_ip", lambda url, client=None: next(replies))
    guard = ng.order_guard(replace(settings, static_ip=IP))
    assert guard() is None
    assert OTHER in guard()
