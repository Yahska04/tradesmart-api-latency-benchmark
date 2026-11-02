"""Tests for ``python -m src.doctor``: PASS/WARN/FAIL logic, exit codes, and that no secret is ever printed."""
from __future__ import annotations

import os
from dataclasses import replace

import pytest

from src import doctor, network_guard, outbound, token_store
from src.database import Database
from src.network_guard import EgressCheck
from src.settings import Settings

IP = "203.0.113.10"
SECRETS = dict(app_key="dummy-app-key-value", secret_key="dummy-secret-key-value", client_id="DUMMYCLIENT1",
               access_token="dummy-access-token-value", auth_proxy_secret="dummy-proxy-secret-value")


@pytest.fixture
def healthy(tmp_path) -> Settings:
    return Settings(base_url="https://mock.tradesmart.test/api", ws_url="wss://x.test", oauth_url="https://x.test/o",
                    redirect_url="", environment="live", static_ip=IP, db_path=tmp_path / "data" / "t.db",
                    log_path=tmp_path / "logs" / "a.log", deployment="server", admin_users=("alice",),
                    order_users=("bob",), enforce_static_ip=True, token_file=tmp_path / "data" / ".access_token",
                    **SECRETS)


@pytest.fixture(autouse=True)
def network(monkeypatch):
    """Everything reachable and matching, unless a test says otherwise."""
    monkeypatch.setattr(doctor, "connectivity_check", lambda base_url, timeout_s: {
        "ok": True, "host": "mock.tradesmart.test", "ip": "192.0.2.1", "error": None})
    monkeypatch.setattr(network_guard, "check_egress", lambda settings, db=None: EgressCheck(
        expected=IP, detected=IP, enforced=settings.enforce_static_ip))
    yield
    outbound.set_local_address(None)


def levels(results) -> dict[str, str]:
    return {name: level for level, name, _ in results}


def detail(results, needle: str) -> str:
    return next(d for _, name, d in results if needle in name)


def level_of(results, needle: str) -> str:
    return next(level for level, name, _ in results if needle in name)


def assert_no_secrets(text: str, *more: str) -> None:
    for secret in (*SECRETS.values(), *more):
        assert secret not in text


def test_healthy_server_has_no_failures(healthy):
    results = doctor.run_checks(healthy)
    assert doctor.FAIL not in levels(results).values()
    assert level_of(results, "Outbound IP") == doctor.PASS


@pytest.mark.parametrize("field,needle,level", [
    ("auth_proxy_secret", "TS_AUTH_PROXY_SECRET", doctor.FAIL),
    ("app_key", "TS_APP_KEY", doctor.FAIL),
    ("secret_key", "TS_SECRET_KEY", doctor.FAIL),
    ("client_id", "TS_CLIENT_ID", doctor.FAIL),
    ("access_token", "Access token", doctor.WARN),
])
def test_missing_value(healthy, field, needle, level):
    assert level_of(doctor.run_checks(replace(healthy, **{field: ""})), needle) == level


def test_missing_admin_users_fails_on_server(healthy):
    assert level_of(doctor.run_checks(replace(healthy, admin_users=())), "TS_ADMIN_USERS") == doctor.FAIL


def test_missing_order_users_only_warns(healthy):
    assert level_of(doctor.run_checks(replace(healthy, order_users=())), "TS_ORDER_USERS") == doctor.WARN


def test_local_mode_skips_server_checks(healthy):
    results = doctor.run_checks(replace(healthy, deployment="local", auth_proxy_secret="", admin_users=()))
    assert not any("TS_AUTH_PROXY_SECRET" in n or "TS_ADMIN_USERS" in n for _, n, _ in results)


def test_token_from_file_counts_on_server(healthy):
    token_store.save_token(healthy.token_file, "dummy-file-token-value", saved_by="alice")
    results = doctor.run_checks(replace(healthy, access_token=""))
    assert level_of(results, "Access token") == doctor.PASS
    assert_no_secrets(str(results), "dummy-file-token-value")


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_loose_token_file_mode_fails(healthy):
    token_store.save_token(healthy.token_file, "x", saved_by="alice")
    healthy.token_file.chmod(0o644)
    assert level_of(doctor.run_checks(healthy), "Token file permissions") == doctor.FAIL


def test_unresolved_orders_fail(healthy):
    Database(healthy.db_path).record_test_orders([{"norenordno": "1"}])
    results = doctor.run_checks(healthy)
    assert level_of(results, "Open test orders") == doctor.FAIL and "1 unconfirmed" in detail(results, "Open test orders")


def test_unusable_database_and_log_fail(healthy, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    results = doctor.run_checks(replace(healthy, db_path=blocker / "t.db", log_path=blocker / "a.log"))
    assert level_of(results, "Database") == doctor.FAIL and level_of(results, "Log file") == doctor.FAIL


def test_unreachable_api_fails(healthy, monkeypatch):
    monkeypatch.setattr(doctor, "connectivity_check", lambda base_url, timeout_s: {
        "ok": False, "host": "x", "ip": None, "error": "connect failed"})
    results = doctor.run_checks(healthy)
    assert level_of(results, "TradeSmart API") == doctor.FAIL and "connect failed" in detail(results, "TradeSmart API")


def test_connectivity_check_is_called_with_the_real_signature(healthy, monkeypatch):
    """No monkeypatched stand-in: the module-level function, stopped by the conftest socket guard."""
    from src.api_client import connectivity_check

    monkeypatch.setattr(doctor, "connectivity_check", connectivity_check)
    results = doctor.run_checks(healthy)
    assert level_of(results, "TradeSmart API") == doctor.FAIL and "dns failed" in detail(results, "TradeSmart API")


@pytest.mark.parametrize("check,deployment,level", [
    (EgressCheck(expected=IP, detected="198.51.100.7", enforced=True), "server", doctor.FAIL),
    (EgressCheck(expected=IP, detected=None, enforced=True, error="timeout"), "server", doctor.FAIL),
    (EgressCheck(expected="", detected=IP, enforced=True), "server", doctor.WARN),
    (EgressCheck(expected=IP, detected="198.51.100.7", enforced=False), "local", doctor.WARN),
])
def test_egress_levels(healthy, monkeypatch, check, deployment, level):
    monkeypatch.setattr(network_guard, "check_egress", lambda settings, db=None: check)
    assert level_of(doctor.run_checks(replace(healthy, deployment=deployment)), "Outbound IP") == level


def test_secrets_in_error_text_are_redacted(healthy, monkeypatch):
    monkeypatch.setattr(doctor, "connectivity_check", lambda base_url, timeout_s: {
        "ok": False, "host": "x", "ip": None, "error": f"failed with {SECRETS['access_token']}"})
    monkeypatch.setattr(network_guard, "check_egress", lambda settings, db=None: EgressCheck(
        expected=IP, detected=None, enforced=True, error=f"bad {SECRETS['auth_proxy_secret']}"))
    assert_no_secrets(str(doctor.run_checks(healthy)))


def test_main_prints_no_secrets_and_exits_zero_when_clean(healthy, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "load_settings", lambda: healthy)
    assert doctor.main() == 0
    out = capsys.readouterr().out
    assert "No failures." in out and "[PASS]" in out
    assert_no_secrets(out)


def test_main_exits_one_on_failure(healthy, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "load_settings", lambda: replace(healthy, admin_users=()))
    assert doctor.main() == 1
    out = capsys.readouterr().out
    assert "1 failure(s)." in out and "[FAIL] Admin users" in out
    assert_no_secrets(out)


def test_main_warnings_alone_exit_zero(healthy, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "load_settings", lambda: replace(healthy, order_users=(), access_token=""))
    assert doctor.main() == 0
    out = capsys.readouterr().out
    assert "[WARN]" in out and "No failures." in out


def test_main_applies_the_outbound_bind_address(healthy, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "load_settings", lambda: replace(healthy, outbound_bind_ip="192.0.2.5"))
    doctor.main()
    assert outbound.local_address() == "192.0.2.5"
