"""Access control and server features of the UI, run through Streamlit's AppTest. No request leaves the process."""
from __future__ import annotations

import ast
import logging
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from src import outbound, redaction, run_lock, token_store
from src import script_master as sm
from src.access import Identity
from src.database import Database
from src.network_guard import STATIC_IP_SETTING, EgressCheck

ROOT = Path(__file__).resolve().parents[1]
VIEWS = sorted((ROOT / "views").glob("*.py"))
SCRIPTS = [ROOT / "app.py", *VIEWS]
SETTINGS = ROOT / "views" / "5_Settings.py"
ORDERS = ROOT / "views" / "9_Order_Latency_Test.py"
REGISTRY = ROOT / "views" / "1_Endpoint_Registry.py"
BENCHMARK = ROOT / "views" / "2_Run_Benchmark.py"
PROXY_SECRET = "ui-test-proxy-secret-value"
ADMIN = Identity("alice", True, is_admin=True, can_place_orders=True)
TRADER = Identity("tara", True, can_place_orders=True)
VIEWER = Identity("victor", True)


def _allow_asyncio_socketpair(monkeypatch) -> None:
    """AppTest creates an event loop, which on Windows connects a loopback socket pair. conftest blocks every
    connect, so let that one internal call through; all other connects stay blocked."""
    blocked = socket.socket.connect

    def connect(self, *args, **kwargs):
        if sys._getframe(1).f_code.co_name == "_fallback_socketpair":
            return super(socket.socket, self).connect(*args, **kwargs)
        return blocked(self, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)


def _clear_caches() -> None:
    st.cache_resource.clear()
    st.cache_data.clear()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Server deployment with throwaway paths and dummy credentials; everything is restored afterwards."""
    values = {
        "TS_DEPLOYMENT": "server", "TS_DB_PATH": str(tmp_path / "bench.db"), "TS_LOG_PATH": str(tmp_path / "app.log"),
        "TS_BASE_URL": "http://127.0.0.1:9/api", "TS_WS_URL": "ws://127.0.0.1:9/ws",
        "TS_OAUTH_URL": "http://127.0.0.1:9/oauth", "TS_ACCESS_TOKEN": "ui-test-env-access-token",
        "TS_CLIENT_ID": "UITESTCLIENT", "TS_APP_KEY": "ui-test-app-key-value", "TS_SECRET_KEY": "ui-test-secret-value",
        "TS_REDIRECT_URL": "", "TS_ENVIRONMENT": "mock", "TS_STATIC_IP": "", "TS_AUTH_PROXY_SECRET": PROXY_SECRET,
        "TS_ADMIN_USERS": "alice", "TS_ORDER_USERS": "alice,tara", "TS_ENFORCE_STATIC_IP": "", "TS_OUTBOUND_BIND_IP": "",
        "TS_IP_CHECK_URL": "http://127.0.0.1:9/", "TS_TOKEN_FILE": str(tmp_path / "token"),
        "TS_SCRIPT_MASTER_AUTOSTART": "0",
    }
    _allow_asyncio_socketpair(monkeypatch)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sm, "_storage_dir", sm._storage_dir)
    monkeypatch.setattr(redaction, "_registered", set(redaction._registered))
    logger = logging.getLogger("tsbench")
    handlers, propagate, level = list(logger.handlers), logger.propagate, logger.level
    _clear_caches()
    yield tmp_path
    _clear_caches()
    outbound.set_local_address(None)
    for handler in [h for h in logger.handlers if h not in handlers]:
        logger.removeHandler(handler)
        handler.close()
    logger.propagate, logger.level = propagate, level


def _as(monkeypatch, identity: Identity) -> None:
    monkeypatch.setattr("src.ui.state.current_identity", lambda: identity)


def _good_ip(monkeypatch, ip: str = "8.8.8.8") -> None:
    monkeypatch.setattr("src.ui.state.egress_status", lambda: EgressCheck(ip, ip, True))


def _open(path: Path, **query) -> AppTest:
    at = AppTest.from_file(str(path), default_timeout=60)
    for key, value in query.items():
        at.query_params[key] = value
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _rerun(at: AppTest) -> AppTest:
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _button(at: AppTest, label: str):
    return next(b for b in at.button if b.label.startswith(label))


def _labels(at: AppTest) -> list[str]:
    return [b.label for b in at.button]


def _text_input(at: AppTest, label: str):
    return next(t for t in at.text_input if t.label.startswith(label))


def _all_text(at: AppTest) -> str:
    kinds = ("markdown", "caption", "error", "warning", "info", "success", "code", "title", "subheader")
    return "\n".join(str(e.value) for kind in kinds for e in getattr(at, kind))


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "bench.db")


# ---------------------------------------------------------------------------- everyone logs in
def test_every_view_calls_apply_theme():
    assert VIEWS, "no views found"
    for path in VIEWS:
        calls = [n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                 if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "apply_theme"]
        assert calls, f"{path.name} must call apply_theme, which enforces access"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_server_without_headers_shows_only_denied(env, script):
    at = _open(script)
    assert any("Access denied" in e.value for e in at.error)
    assert "system team" in at.error[0].value
    assert not at.title and not at.subheader and not at.tabs and not at.button
    assert not at.text_input and not at.selectbox and not at.metric and not at.dataframe


def test_server_rejects_wrong_proxy_secret(env, monkeypatch):
    monkeypatch.setattr("src.ui.state._request_headers",
                        lambda: {"X-Remote-User": "alice", "X-Proxy-Secret": "wrong"})
    at = _open(SETTINGS)
    assert any("login proxy" in e.value for e in at.error)
    assert not at.tabs


def test_server_with_proxy_headers_signs_in_and_names_the_user(env, monkeypatch):
    monkeypatch.setattr("src.ui.state._request_headers",
                        lambda: {"X-Remote-User": "alice", "X-Proxy-Secret": PROXY_SECRET})
    at = _open(SETTINGS)
    assert at.tabs
    sidebar = "\n".join(c.value for c in at.sidebar.caption)
    assert "Signed in as **alice**" in sidebar and "admin" in sidebar and "can place orders" in sidebar


def test_settings_applies_outbound_bind_ip(env, monkeypatch):
    monkeypatch.setenv("TS_OUTBOUND_BIND_IP", "10.1.2.3")
    from src.ui.state import get_settings

    get_settings()
    assert outbound.local_address() == "10.1.2.3"


# ---------------------------------------------------------------------------- roles
def test_non_admin_is_blocked_from_settings(env, monkeypatch):
    _as(monkeypatch, VIEWER)
    at = _open(SETTINGS, code="one-time-code")
    assert any("Only admins" in w.value for w in at.warning)
    assert not at.tabs and not at.button and "code" not in at.query_params


def test_order_page_needs_order_permission(env, monkeypatch):
    _as(monkeypatch, VIEWER)
    at = _open(ORDERS)
    assert any("order access list" in w.value for w in at.warning)
    assert not at.button and not at.text_input and not at.toggle and "REAL orders" not in _all_text(at)


def test_registry_is_read_only_for_non_admins(env, monkeypatch):
    _as(monkeypatch, VIEWER)
    at = _open(REGISTRY)
    assert at.selectbox and "Only admins can change the registry." in _all_text(at)
    for label in ("Save registry", "Discard changes", "Validate import", "Save manual result"):
        assert label not in _labels(at)
    assert not [t for t in at.text_area if t.label.startswith("How was this measured")]


def test_registry_is_editable_for_admins(env, monkeypatch):
    _as(monkeypatch, ADMIN)
    at = _open(REGISTRY)
    for label in ("Save registry", "Discard changes", "Validate import", "Save manual result"):
        assert label in _labels(at)


@pytest.mark.parametrize("script", [ROOT / "app.py", ROOT / "views" / "3_Performance_Analytics.py",
                                    ROOT / "views" / "4_Historical_Comparison.py"], ids=lambda p: p.name)
def test_load_demo_data_is_admin_only(env, monkeypatch, script):
    _as(monkeypatch, VIEWER)
    assert "Load demo data" not in _labels(_open(script))
    _as(monkeypatch, ADMIN)
    assert "Load demo data" in _labels(_open(script))


# ---------------------------------------------------------------------------- Settings → Server IP
def test_server_ip_tab_is_first_and_works_blank(env, monkeypatch):
    _as(monkeypatch, ADMIN)
    at = _open(SETTINGS)
    assert at.tabs[0].label == "Server IP"
    assert _text_input(at, "Server static IP").value == ""
    text = _all_text(at)
    assert "NOT SET" in text and "Orders stay blocked until it is set." in text
    assert "/callback" not in text


def test_admin_saves_and_clears_the_ip(env, monkeypatch):
    _as(monkeypatch, ADMIN)
    at = _open(SETTINGS)
    _text_input(at, "Server static IP").set_value("8.8.8.8")
    _button(at, "Save IP").click()
    _rerun(at)
    assert _db(env).get_setting(STATIC_IP_SETTING) == "8.8.8.8"
    assert any("8.8.8.8" in s.value for s in at.success)
    assert _text_input(at, "Server static IP").value == "8.8.8.8"
    assert "http://8.8.8.8/callback" in _all_text(at)
    assert "CAN&#x27;T DETECT" in _all_text(at)  # tests block sockets, so the check fails closed

    _text_input(at, "Server static IP").set_value("")
    _button(at, "Save IP").click()
    _rerun(at)
    assert not _db(env).get_setting(STATIC_IP_SETTING)
    assert "NOT SET" in _all_text(at)


def test_bad_ip_is_refused_and_private_ip_warns(env, monkeypatch):
    _as(monkeypatch, ADMIN)
    at = _open(SETTINGS)
    _text_input(at, "Server static IP").set_value("not-an-ip")
    _button(at, "Save IP").click()
    _rerun(at)
    assert any("isn't a valid IP address" in e.value for e in at.error)
    assert _db(env).get_setting(STATIC_IP_SETTING) is None

    _text_input(at, "Server static IP").set_value("10.0.0.5")
    _button(at, "Save IP").click()
    _rerun(at)
    assert _db(env).get_setting(STATIC_IP_SETTING) == "10.0.0.5"
    assert any("isn't a public IP" in w.value for w in at.warning)


def test_check_now_runs_a_new_check(env, monkeypatch):
    _as(monkeypatch, ADMIN)
    calls = []

    def fake_check(settings, db=None, **_):
        calls.append(1)
        return EgressCheck("", "8.8.8.8", True)

    monkeypatch.setattr("src.network_guard.check_egress", fake_check)
    at = _open(SETTINGS)
    _rerun(at)
    assert len(calls) == 1                       # cached between page runs
    _button(at, "Check now").click()
    _rerun(at)
    assert len(calls) == 2


def test_environment_tab_lists_new_settings_and_masks_the_secret(env, monkeypatch):
    _as(monkeypatch, ADMIN)
    at = _open(SETTINGS)
    frame = at.tabs[1].dataframe[0].value
    settings = list(frame["Setting"])
    assert "Static IP (.env fallback)" in settings and "Users who can place orders" in settings
    assert PROXY_SECRET not in frame.to_string()
    row = frame[frame["Env variable"] == "TS_ADMIN_USERS"].iloc[0]
    assert row["Value (secrets masked)"] == "alice"


# ---------------------------------------------------------------------------- token on the server
def _fake_exchange(monkeypatch, token: str = "ui-test-new-token-value"):
    from src.auth import TokenResult

    seen: list[str] = []

    def fake(settings, code, **_):
        seen.append(code)
        return TokenResult(True, token, None, ["susertoken"])

    monkeypatch.setattr("src.auth.generate_access_token", fake)
    return seen


def test_callback_code_needs_a_click_and_is_never_shown(env, monkeypatch):
    _as(monkeypatch, ADMIN)
    seen = _fake_exchange(monkeypatch)
    at = _open(SETTINGS, code="abc def")          # "+" arrives as a space after query decoding
    assert any("TradeSmart sent a login code" in m.value for m in at.markdown)
    assert not seen, "must not exchange on page load"
    assert "abc" not in _all_text(at)

    next(b for b in at.button if b.key == "callback_get_token").click()
    _rerun(at)
    assert seen == ["abc+def"]
    assert "code" not in at.query_params
    assert not [b for b in at.button if b.key == "callback_get_token"]
    assert token_store.load_token(env / "token") == "ui-test-new-token-value"
    info = token_store.token_info(env / "token")
    assert info["saved_by"] == "alice"
    text = _all_text(at)
    assert "Saved by alice" in text and "ui-test-new-token-value" not in text and "abc" not in text


def test_token_is_shared_between_sessions_and_can_be_cleared(env, monkeypatch):
    _as(monkeypatch, ADMIN)
    _fake_exchange(monkeypatch)
    at = _open(SETTINGS)
    assert "shared by all users" in _all_text(at) and "owner-only permissions" in _all_text(at)
    _text_input(at, "2 · Paste the redirect URL").set_value("pasted-code")
    next(b for b in at.button if b.label == "Get access token").click()
    _rerun(at)
    assert token_store.load_token(env / "token") == "ui-test-new-token-value"
    assert "ui-test-new-token-value" in redaction.registered_secrets()
    assert "oauth_access_token" not in at.session_state

    _as(monkeypatch, VIEWER)
    other = _open(BENCHMARK)
    assert "the shared token on this server" in _all_text(other)

    _as(monkeypatch, ADMIN)
    _rerun(at)
    _button(at, "Clear shared token").click()
    _rerun(at)
    assert token_store.load_token(env / "token") == ""


def test_local_token_stays_in_the_session(env, monkeypatch):
    monkeypatch.setenv("TS_DEPLOYMENT", "local")
    _fake_exchange(monkeypatch)
    at = _open(SETTINGS)
    assert "stays in this browser session only" in _all_text(at)
    _text_input(at, "2 · Paste the redirect URL").set_value("pasted-code")
    next(b for b in at.button if b.label == "Get access token").click()
    _rerun(at)
    assert at.session_state["oauth_access_token"] == "ui-test-new-token-value"
    assert not (env / "token").exists()


# ---------------------------------------------------------------------------- order page
def _place_button(at: AppTest):
    return _button(at, "Place and cancel")


def _ready_order_form(monkeypatch, env) -> AppTest:
    """Open the order page with a valid form: symbol, token, preview and typed confirmation."""
    from src.order_test import ALLOWED_EXCHANGES, ALLOWED_PRODUCTS, DEFAULT_OFFSET_PCT, confirmation_phrase

    at = _open(ORDERS)
    at.toggle(key="order_manual").set_value(True)
    _rerun(at)
    _text_input(at, "Trading symbol").set_value("ABC-EQ")
    _text_input(at, "Instrument token").set_value("123")
    at.session_state["order_test_preview"] = {
        "key": (ALLOWED_EXCHANGES[0], "ABC-EQ", "123", list(ALLOWED_PRODUCTS)[0], DEFAULT_OFFSET_PCT),
        "tsym": "ABC-EQ", "ltp": 100.0, "price": 90.0, "lc": None, "fetched": None}
    _rerun(at)
    count = int(at.number_input[0].value)
    phrase = confirmation_phrase(count)
    next(t for t in at.text_input if phrase in t.label).set_value(phrase)
    _rerun(at)
    return at


def test_order_page_blocks_placing_when_ip_is_not_set(env, monkeypatch):
    _as(monkeypatch, TRADER)
    at = _open(ORDERS)
    assert any("Orders are blocked" in e.value and "has not been set" in e.value for e in at.error)
    assert _place_button(at).disabled
    assert "fix the server IP problem above" in _all_text(at)
    assert "Outbound IP" in _all_text(at)


def test_order_form_enables_placing_with_a_matching_ip(env, monkeypatch):
    _as(monkeypatch, TRADER)
    _good_ip(monkeypatch)
    at = _ready_order_form(monkeypatch, env)
    assert not _place_button(at).disabled


def test_ip_problem_disables_a_complete_form(env, monkeypatch):
    _as(monkeypatch, TRADER)
    _good_ip(monkeypatch)
    at = _ready_order_form(monkeypatch, env)
    monkeypatch.setattr("src.ui.state.egress_status", lambda: EgressCheck("8.8.8.8", "1.1.1.1", True))
    _rerun(at)
    assert _place_button(at).disabled


def test_another_run_disables_placing(env, monkeypatch):
    _as(monkeypatch, TRADER)
    _good_ip(monkeypatch)
    at = _ready_order_form(monkeypatch, env)
    with run_lock.exclusive_run(run_lock.lock_path_for(env / "bench.db"), owner="bob", kind="benchmark"):
        _rerun(at)
        assert _place_button(at).disabled
        assert any("started by bob" in w.value for w in at.warning)
        assert _button(at, "Check for open test orders").disabled
    _rerun(at)
    assert not _place_button(at).disabled


def test_another_run_disables_starting_a_benchmark(env, monkeypatch):
    _as(monkeypatch, VIEWER)
    monkeypatch.setattr("src.ui.state.egress_status", lambda: EgressCheck("", None, True))
    with run_lock.exclusive_run(run_lock.lock_path_for(env / "bench.db"), owner="bob", kind="order test"):
        at = _open(BENCHMARK)
        assert _button(at, "Start benchmark").disabled
        assert any("started by bob" in w.value for w in at.warning)
    assert any("has not been set" in w.value for w in at.warning)


def test_open_orders_block_placing_and_can_be_checked(env, monkeypatch):
    from src.order_test import ReconcileReport

    _as(monkeypatch, TRADER)
    _good_ip(monkeypatch)
    db = _db(env)
    db.record_test_orders([{"norenordno": "111", "run_id": "order-x", "exch": "NSE", "tsym": "ABC-EQ",
                            "started_by": "tara"}])
    at = _ready_order_form(monkeypatch, env)
    assert any("1 test order is not confirmed closed" in e.value for e in at.error)
    assert _place_button(at).disabled
    assert "check the open test orders above" in _all_text(at)

    calls = []

    def fake_reconcile(database, eps, client, *, client_id, started_by):
        calls.append(started_by)
        database.update_test_order("111", "closed", "CANCELED")
        return ReconcileReport(checked=1, cancelled=["111"])

    monkeypatch.setattr("src.order_test.reconcile_test_orders", fake_reconcile)
    _button(at, "Check for open test orders").click()
    _rerun(at)
    assert calls == ["tara"]
    assert not any("not confirmed closed" in e.value for e in at.error)
    assert any("No test orders are left open" in s.value for s in at.success)
    assert "cancelled 1" in _all_text(at)
    assert not _place_button(at).disabled


def test_failed_open_order_check_reports_what_needs_attention(env, monkeypatch):
    from src.order_test import ReconcileReport

    _as(monkeypatch, TRADER)
    _good_ip(monkeypatch)
    at = _open(ORDERS)
    monkeypatch.setattr("src.order_test.reconcile_test_orders", lambda *a, **k: ReconcileReport(
        checked=2, filled=["222"], still_open=["333"], errors=["Cancel failed for 333: boom"]))
    _button(at, "Check for open test orders").click()
    _rerun(at)
    errors = "\n".join(e.value for e in at.error)
    assert "222" in errors and "333" in errors and "boom" in errors


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_batch_is_shared_by_order_users_and_reports_who_started_it(env, monkeypatch):
    from src.order_test import OrderTestOutcome

    started, finished = threading.Event(), threading.Event()
    seen: dict = {}

    def fake_run(db, eps, client, *, stop_event, started_by, ip_guard, on_attempt, run_id, **_):
        seen.update(started_by=started_by, ip_guard=ip_guard)
        started.set()
        assert stop_event.wait(10)
        finished.set()
        return OrderTestOutcome(run_id=run_id, started_by=started_by, stop_reason="Stopped by a user.")

    monkeypatch.setattr("src.order_test.run_order_test", fake_run)
    _as(monkeypatch, ADMIN)
    _good_ip(monkeypatch)
    at = _ready_order_form(monkeypatch, env)
    _place_button(at).click()
    _rerun(at)
    assert started.wait(10)
    assert seen["started_by"] == "alice" and callable(seen["ip_guard"])
    assert at.session_state["order_batches"] == 1

    _as(monkeypatch, TRADER)                       # a second user, in a separate browser session
    other = _open(ORDERS)
    assert any("started by alice" in i.value for i in other.info)
    assert not [b for b in other.button if b.label.startswith("Place and cancel")]
    _button(other, "Stop the batch").click()
    _rerun(other)
    assert finished.wait(10)

    # The worker clears the job right after it finishes.
    def idle() -> bool:
        _rerun(other)
        return not any("is running" in i.value for i in other.info)

    _wait_for(idle)
    assert "Last batch, started by alice." in _all_text(other)
    assert any("Stopped by a user." in w.value for w in other.warning)

    _as(monkeypatch, ADMIN)
    _rerun(at)
    assert not [t for t in at.text_input if t.key == "order_confirm_0"]
    assert next(t for t in at.text_input if t.key == "order_confirm_1").value == ""
    assert _place_button(at).disabled


def test_worker_refusal_is_shown_to_order_users(env, monkeypatch):
    from src.order_test import OrderTestError

    def fake_run(*_, **__):
        raise OrderTestError("the server IP changed")

    monkeypatch.setattr("src.order_test.run_order_test", fake_run)
    _as(monkeypatch, ADMIN)
    _good_ip(monkeypatch)
    at = _ready_order_form(monkeypatch, env)
    _place_button(at).click()

    def refused() -> bool:
        _rerun(at)
        return any("the server IP changed" in e.value for e in at.error)

    _wait_for(refused)
    assert any("Batch started by alice" in e.value for e in at.error)


# ---------------------------------------------------------------------------- local deployment
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_local_pages_run_without_login(env, monkeypatch, script):
    monkeypatch.setenv("TS_DEPLOYMENT", "local")
    at = _open(script)
    assert not any("Access denied" in e.value for e in at.error)
    assert not [c for c in at.sidebar.caption if "Signed in as" in c.value]


def test_local_settings_and_orders_keep_full_access(env, monkeypatch):
    monkeypatch.setenv("TS_DEPLOYMENT", "local")
    at = _open(SETTINGS)
    assert at.tabs[0].label == "Server IP"
    orders = _open(ORDERS)
    assert not any("Orders are blocked" in e.value for e in orders.error)   # enforcement is off locally
    assert orders.number_input
