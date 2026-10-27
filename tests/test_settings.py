"""Tests for src.settings: defaults, .env loading, env precedence, masking."""
from __future__ import annotations

import pytest

from src.settings import PROJECT_ROOT, SECRET_FIELDS, load_settings

ENV_VARS = ("TS_BASE_URL", "TS_WS_URL", "TS_OAUTH_URL", "TS_APP_KEY", "TS_SECRET_KEY", "TS_CLIENT_ID",
            "TS_REDIRECT_URL", "TS_ACCESS_TOKEN", "TS_ENVIRONMENT", "TS_STATIC_IP", "TS_DB_PATH", "TS_LOG_PATH")


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
