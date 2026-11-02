"""Tests for ``python -m src.recover_orders``: exit codes and token sources, with no real network or settings."""
from __future__ import annotations

import logging

import httpx
import pytest

from src import outbound, recover_orders, token_store
from src.api_client import TradeSmartClient
from src.database import Database
from src.settings import load_settings

NUM = "25101900000001"
TOKEN = "dummy-recovery-token"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Point settings at tmp paths, away from the real ``.env`` and database. No global logger or secret changes."""
    for name in ("TS_ACCESS_TOKEN", "TS_CLIENT_ID", "TS_DEPLOYMENT", "TS_TOKEN_FILE", "TS_OUTBOUND_BIND_IP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TS_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TS_LOG_PATH", str(tmp_path / "a.log"))
    monkeypatch.setenv("TS_BASE_URL", "https://mock.tradesmart.test/api")
    monkeypatch.setattr(recover_orders, "load_settings", lambda: load_settings(env_file=tmp_path / "missing.env"))
    monkeypatch.setattr(recover_orders, "setup_logging", lambda *a, **k: logging.getLogger("tsbench.test"))
    monkeypatch.setattr(recover_orders, "register_secrets", lambda secrets: None)
    yield
    outbound.set_local_address(None)


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


class Broker:
    def __init__(self, status="OPEN", book=None):
        self.status, self.book, self.calls, self.tokens = status, book or [], [], []

    def __call__(self, request):
        name = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(name)
        self.tokens.append(request.headers.get("authorization"))
        if name == "OrderBook":
            return httpx.Response(200, json=self.book)
        if name == "SingleOrdHist":
            return httpx.Response(200, json=[{"status": self.status, "fillshares": "0"}])
        if name == "CancelOrder":
            self.status = "CANCELED"
            return httpx.Response(200, json={"stat": "Ok"})
        raise AssertionError(name)


def install(monkeypatch, broker):
    def factory(base_url, access_token="", **kw):
        return TradeSmartClient(base_url, access_token=access_token, transport=httpx.MockTransport(broker), **kw)

    monkeypatch.setattr(recover_orders, "TradeSmartClient", factory)
    monkeypatch.setattr("src.order_test.STATUS_POLL_INTERVAL_S", 0)


def journal(db):
    db.record_test_orders([{"norenordno": NUM, "run_id": "r1", "exch": "NSE", "tsym": "IDEA-EQ", "started_by": "a"}])


def credentials(monkeypatch):
    monkeypatch.setenv("TS_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("TS_CLIENT_ID", "AB1234")


def test_no_token_and_nothing_unresolved_is_clean(db, capsys):
    assert recover_orders.main() == 0
    assert "No access token" in capsys.readouterr().out


def test_no_token_with_unresolved_orders_needs_attention(db, capsys):
    journal(db)
    assert recover_orders.main() == 1
    assert "ATTENTION: 1 test order" in capsys.readouterr().out


def test_token_but_no_client_id_counts_as_no_token(db, monkeypatch):
    monkeypatch.setenv("TS_ACCESS_TOKEN", TOKEN)
    journal(db)
    assert recover_orders.main() == 1


def test_clean_run_returns_zero(db, monkeypatch):
    credentials(monkeypatch)
    broker = Broker()
    install(monkeypatch, broker)
    assert recover_orders.main() == 0
    assert broker.calls == ["OrderBook"]


def test_cancels_a_leftover_order_and_returns_zero(db, monkeypatch, capsys):
    credentials(monkeypatch)
    broker = Broker()
    install(monkeypatch, broker)
    journal(db)
    assert recover_orders.main() == 0
    assert "CancelOrder" in broker.calls and db.unresolved_test_orders() == []
    out = capsys.readouterr().out
    assert "1 cancelled" in out and TOKEN not in out


def test_filled_order_returns_one(db, monkeypatch, capsys):
    credentials(monkeypatch)
    broker = Broker(status="COMPLETE")
    install(monkeypatch, broker)
    journal(db)
    assert recover_orders.main() == 1
    assert f"ATTENTION: check order {NUM}" in capsys.readouterr().out


def test_order_that_stays_open_returns_one(db, monkeypatch, capsys):
    credentials(monkeypatch)

    class Stuck(Broker):
        def __call__(self, request):
            response = super().__call__(request)
            self.status = "OPEN"
            return response

    install(monkeypatch, Stuck())
    journal(db)
    assert recover_orders.main() == 1
    assert len(db.unresolved_test_orders()) == 1


def test_busy_lock_is_skipped_quietly_when_nothing_is_unresolved(db, monkeypatch, capsys):
    from src import run_lock

    credentials(monkeypatch)
    install(monkeypatch, Broker())
    with run_lock.exclusive_run(run_lock.lock_path_for(db.path), owner="alice", kind="benchmark"):
        assert recover_orders.main() == 0
    assert "Skipped" in capsys.readouterr().out


def test_busy_lock_with_unresolved_orders_needs_attention(db, monkeypatch):
    from src import run_lock

    credentials(monkeypatch)
    broker = Broker()
    install(monkeypatch, broker)
    journal(db)
    with run_lock.exclusive_run(run_lock.lock_path_for(db.path), owner="alice", kind="benchmark"):
        assert recover_orders.main() == 1
    assert broker.calls == []


def test_server_prefers_the_token_file(db, tmp_path, monkeypatch):
    credentials(monkeypatch)
    monkeypatch.setenv("TS_DEPLOYMENT", "server")
    token_store.save_token(tmp_path / ".access_token", "file-token-value", saved_by="admin")
    broker = Broker()
    install(monkeypatch, broker)
    assert recover_orders.main() == 0
    assert broker.tokens == ["Bearer file-token-value"]


def test_server_uses_the_token_file_alone(db, tmp_path, monkeypatch):
    monkeypatch.setenv("TS_CLIENT_ID", "AB1234")
    monkeypatch.setenv("TS_DEPLOYMENT", "server")
    token_store.save_token(tmp_path / ".access_token", "file-token-value", saved_by="admin")
    broker = Broker()
    install(monkeypatch, broker)
    assert recover_orders.main() == 0 and broker.calls == ["OrderBook"]


def test_local_ignores_the_token_file(db, tmp_path, monkeypatch):
    monkeypatch.setenv("TS_CLIENT_ID", "AB1234")
    token_store.save_token(tmp_path / ".access_token", "file-token-value", saved_by="admin")
    broker = Broker()
    install(monkeypatch, broker)
    assert recover_orders.main() == 0 and broker.calls == []


def test_applies_the_outbound_bind_address(db, monkeypatch):
    monkeypatch.setenv("TS_OUTBOUND_BIND_IP", "192.0.2.5")
    assert recover_orders.main() == 0
    assert outbound.local_address() == "192.0.2.5"
