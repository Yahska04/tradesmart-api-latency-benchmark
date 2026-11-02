"""Tests for src.access: who is using the app and what they may do. Server mode must fail closed."""
from __future__ import annotations

from dataclasses import replace

import pytest

from src.access import LOCAL_USER, SECRET_HEADER, USER_HEADER, resolve_identity
from src.settings import Settings, load_settings

SECRET = "proxy-shared-secret-value"


def make(tmp_path, **kw) -> Settings:
    base = Settings(base_url="https://x.test", ws_url="wss://x.test", oauth_url="https://x.test/o", app_key="", secret_key="",
                    client_id="", redirect_url="", access_token="", environment="mock", static_ip="",
                    db_path=tmp_path / "t.db", log_path=tmp_path / "a.log")
    return replace(base, **kw)


@pytest.fixture
def server(tmp_path):
    return make(tmp_path, deployment="server", auth_proxy_secret=SECRET, admin_users=("Alice",),
                order_users=("alice", "BOB"))


def test_local_identity_has_every_permission_without_headers(tmp_path):
    me = resolve_identity(make(tmp_path), None)
    assert me.username == LOCAL_USER and me.authenticated and me.is_admin and me.can_place_orders


def test_local_ignores_headers(tmp_path):
    me = resolve_identity(make(tmp_path), {USER_HEADER: "mallory"})
    assert me.username == LOCAL_USER and me.is_admin


def test_server_denies_when_secret_is_not_configured(tmp_path):
    settings = make(tmp_path, deployment="server", admin_users=("alice",))
    # an empty supplied secret must not "match" an empty configured secret
    me = resolve_identity(settings, {USER_HEADER: "alice", SECRET_HEADER: ""})
    assert not me.authenticated and not me.is_admin and "TS_AUTH_PROXY_SECRET" in me.denied_reason


@pytest.mark.parametrize("headers", [
    None,
    {},
    {SECRET_HEADER: "wrong"},
    {USER_HEADER: "alice"},                         # forged user header, no secret
    {USER_HEADER: "alice", SECRET_HEADER: "wrong"},
    {USER_HEADER: "alice", SECRET_HEADER: ""},
    {USER_HEADER: "alice", SECRET_HEADER: SECRET + "x"},
    {USER_HEADER: "alice", SECRET_HEADER: "é"},   # non-ASCII must be a denial, not a crash
])
def test_server_denies_wrong_or_missing_secret(server, headers):
    me = resolve_identity(server, headers)
    assert not me.authenticated and not me.is_admin and not me.can_place_orders
    assert me.username == "" and me.denied_reason


@pytest.mark.parametrize("user", ["", "   "])
def test_server_denies_empty_user_header(server, user):
    me = resolve_identity(server, {USER_HEADER: user, SECRET_HEADER: SECRET})
    assert not me.authenticated and "did not identify" in me.denied_reason


def test_server_denies_missing_user_header(server):
    assert not resolve_identity(server, {SECRET_HEADER: SECRET}).authenticated


def test_correct_secret_and_user_authenticates(server):
    me = resolve_identity(server, {USER_HEADER: "carol", SECRET_HEADER: SECRET})
    assert me.authenticated and me.username == "carol" and me.denied_reason == ""
    assert not me.is_admin and not me.can_place_orders          # logged in, but in neither list


def test_header_names_are_case_insensitive(server):
    me = resolve_identity(server, {"X-Remote-User": " alice ", "X-Proxy-Secret": SECRET})
    assert me.authenticated and me.username == "alice"


@pytest.mark.parametrize("user,admin,orders", [
    ("alice", True, True), ("ALICE", True, True), ("bob", False, True), ("Bob", False, True), ("alicia", False, False),
])
def test_admin_and_order_lists_are_case_insensitive(server, user, admin, orders):
    me = resolve_identity(server, {USER_HEADER: user, SECRET_HEADER: SECRET})
    assert (me.is_admin, me.can_place_orders) == (admin, orders)


def test_unknown_deployment_resolves_to_server_and_denies(tmp_path, monkeypatch):
    monkeypatch.delenv("TS_AUTH_PROXY_SECRET", raising=False)
    monkeypatch.setenv("TS_DEPLOYMENT", "prodution")
    settings = load_settings(env_file=tmp_path / "missing.env")
    assert settings.is_server()
    assert not resolve_identity(settings, {USER_HEADER: "alice"}).authenticated
