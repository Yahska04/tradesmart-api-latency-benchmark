"""Tests for src.settings: defaults, .env loading, env precedence, masking."""
from __future__ import annotations

import pytest

from src.settings import PROJECT_ROOT, SECRET_FIELDS, load_settings

ENV_VARS = ("TS_BASE_URL", "TS_WS_URL", "TS_OAUTH_URL", "TS_APP_KEY", "TS_SECRET_KEY", "TS_CLIENT_ID",
            "TS_REDIRECT_URL", "TS_ACCESS_TOKEN", "TS_ENVIRONMENT", "TS_STATIC_IP", "TS_DB_PATH", "TS_LOG_PATH",
            "TS_DEPLOYMENT", "TS_AUTH_PROXY_SECRET", "TS_ADMIN_USERS", "TS_ORDER_USERS", "TS_ENFORCE_STATIC_IP",
            "TS_OUTBOUND_BIND_IP", "TS_IP_CHECK_URL", "TS_TOKEN_FILE")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_without_env_file(tmp_path):
    s = load_settings(env_file=tmp_path / "missing.env")
    assert s.base_url == "https://v2api.tradesmartonline.in/NorenWClientAPIv2"
    assert s.ws_url == "wss://v2api.tradesmartonline.in/NorenWSAPI/"
    assert s.oauth_url.endswith("/OAuthlogin/authorize/oauth")
    assert s.environment == "live" and s.is_live()
    assert s.access_token == "" and not s.has_credentials()
    assert s.db_path == PROJECT_ROOT / "data" / "benchmark.db"
    assert s.log_path == PROJECT_ROOT / "data" / "logs" / "app.log"
    assert s.secret_values() == []


def test_env_file_and_env_precedence(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "TS_ACCESS_TOKEN=file-token-123\nTS_CLIENT_ID=FA00001\nTS_ENVIRONMENT=uat\n"
        "TS_APP_KEY=file-app-key\nTS_DB_PATH=custom/db.sqlite\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TS_ACCESS_TOKEN", "env-token-456")
    s = load_settings(env_file=env)
    assert s.access_token == "env-token-456"  # real env wins
    assert s.client_id == "FA00001"
    assert s.environment == "uat" and not s.is_live()
    assert s.has_credentials()
    assert s.db_path == PROJECT_ROOT / "custom" / "db.sqlite"
    assert set(s.secret_values()) == {"env-token-456", "FA00001", "file-app-key"}


def test_invalid_environment_falls_back_to_live(tmp_path, monkeypatch):
    monkeypatch.setenv("TS_ENVIRONMENT", "PRODUCTION??")
    assert load_settings(env_file=tmp_path / "x.env").environment == "live"
    monkeypatch.setenv("TS_ENVIRONMENT", "MOCK")
    assert load_settings(env_file=tmp_path / "x.env").environment == "mock"


def test_absolute_paths_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("TS_DB_PATH", str(tmp_path / "abs.db"))
    assert load_settings(env_file=tmp_path / "x.env").db_path == tmp_path / "abs.db"


def test_masked_and_repr_hide_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TS_ACCESS_TOKEN", "averylongaccesstoken")
    monkeypatch.setenv("TS_SECRET_KEY", "supersecretkeyvalue")
    monkeypatch.setenv("TS_CLIENT_ID", "FA12345")
    s = load_settings(env_file=tmp_path / "x.env")
    masked = s.masked()
    assert set(SECRET_FIELDS) <= set(masked)
    assert masked["access_token"] == "av****en"
    assert "supersecretkeyvalue" not in str(masked)
    assert "FA12345" not in str(masked)
    assert masked["base_url"] == s.base_url
    assert "averylongaccesstoken" not in repr(s) and "supersecretkeyvalue" not in repr(s)


def _load(tmp_path):
    return load_settings(env_file=tmp_path / "missing.env")


def test_local_deployment_is_the_default_and_keeps_enforcement_off(tmp_path):
    s = _load(tmp_path)
    assert s.deployment == "local" and not s.is_server() and s.enforce_static_ip is False


def test_server_enforces_static_ip_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("TS_DEPLOYMENT", "SERVER")
    s = _load(tmp_path)
    assert s.is_server() and s.enforce_static_ip is True


@pytest.mark.parametrize("value", ["0", "false", "No", "OFF"])
def test_explicit_off_turns_enforcement_off_on_server(tmp_path, monkeypatch, value):
    monkeypatch.setenv("TS_DEPLOYMENT", "server")
    monkeypatch.setenv("TS_ENFORCE_STATIC_IP", value)
    assert _load(tmp_path).enforce_static_ip is False


@pytest.mark.parametrize("value", ["1", "true", "typo"])
def test_any_other_value_enforces_even_on_local(tmp_path, monkeypatch, value):
    monkeypatch.setenv("TS_ENFORCE_STATIC_IP", value)
    assert _load(tmp_path).enforce_static_ip is True


def test_unknown_deployment_means_server(tmp_path, monkeypatch):
    monkeypatch.setenv("TS_DEPLOYMENT", "staging")
    s = _load(tmp_path)
    assert s.deployment == "server" and s.is_server() and s.enforce_static_ip is True


def test_user_lists_are_parsed(tmp_path, monkeypatch):
    monkeypatch.setenv("TS_ADMIN_USERS", " alice, bob ,,")
    monkeypatch.setenv("TS_ORDER_USERS", "carol")
    s = _load(tmp_path)
    assert s.admin_users == ("alice", "bob") and s.order_users == ("carol",)
    monkeypatch.delenv("TS_ADMIN_USERS")
    assert _load(tmp_path).admin_users == ()


def test_token_file_defaults_next_to_the_database(tmp_path, monkeypatch):
    monkeypatch.setenv("TS_DB_PATH", str(tmp_path / "store" / "b.db"))
    assert _load(tmp_path).token_file == tmp_path / "store" / ".access_token"
    monkeypatch.setenv("TS_TOKEN_FILE", str(tmp_path / "secret" / "tok"))
    assert _load(tmp_path).token_file == tmp_path / "secret" / "tok"


def test_new_settings_load_from_the_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TS_DEPLOYMENT=server\nTS_OUTBOUND_BIND_IP=192.0.2.5\nTS_IP_CHECK_URL=https://ip.test\n"
                   "TS_ADMIN_USERS=alice\n", encoding="utf-8")
    s = load_settings(env_file=env)
    assert s.is_server() and s.outbound_bind_ip == "192.0.2.5" and s.ip_check_url == "https://ip.test"
    assert s.admin_users == ("alice",)


def test_auth_proxy_secret_is_treated_as_a_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TS_AUTH_PROXY_SECRET", "proxysecretvaluelong")
    s = _load(tmp_path)
    assert "auth_proxy_secret" in SECRET_FIELDS
    assert s.masked()["auth_proxy_secret"] == "pr****ng"
    assert "proxysecretvaluelong" not in repr(s)
    assert s.secret_values() == ["proxysecretvaluelong"]


def test_script_master_autostart_read_like_other_settings(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TS_SCRIPT_MASTER_AUTOSTART=0\n", encoding="utf-8")
    monkeypatch.delenv("TS_SCRIPT_MASTER_AUTOSTART", raising=False)
    assert load_settings(env).script_master_autostart is False
    monkeypatch.setenv("TS_SCRIPT_MASTER_AUTOSTART", "1")
    assert load_settings(env).script_master_autostart is True
