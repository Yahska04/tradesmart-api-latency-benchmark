"""Deployment self-check: ``python -m src.doctor``. Prints PASS/WARN/FAIL lines; exit code 1 if anything FAILs.

Safe to run at any time: it sends no API requests and no credentials. It only checks configuration, file
permissions and network reachability, and it never prints a secret.
"""
from __future__ import annotations

import os
import sqlite3
import stat
import sys
from pathlib import Path

from src import network_guard, outbound, token_store
from src.api_client import connectivity_check
from src.database import Database
from src.endpoint_registry import load_endpoints
from src.redaction import redact_text
from src.order_test import NEEDS_CHECK_SETTING
from src.settings import Settings, load_settings

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def run_checks(settings: Settings) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []

    def add(level: str, name: str, detail: str = "") -> None:
        results.append((level, name, redact_text(detail, settings.secret_values())))

    add(PASS, "Deployment mode", settings.deployment)
    if settings.is_server():
        add(PASS if settings.auth_proxy_secret else FAIL, "Login proxy secret (TS_AUTH_PROXY_SECRET)",
            "set" if settings.auth_proxy_secret else "missing: nobody can log in")
        add(PASS if settings.admin_users else FAIL, "Admin users (TS_ADMIN_USERS)",
            f"{len(settings.admin_users)} configured" if settings.admin_users else "none: Settings page is unreachable")
        add(PASS if settings.order_users else WARN, "Order users (TS_ORDER_USERS)",
            f"{len(settings.order_users)} configured" if settings.order_users else "none: nobody can place test orders")

    for label, value, var in (("App key", settings.app_key, "TS_APP_KEY"), ("Secret key", settings.secret_key, "TS_SECRET_KEY"),
                              ("Client ID", settings.client_id, "TS_CLIENT_ID")):
        add(PASS if value else FAIL, f"{label} ({var})", "set" if value else "missing")

    token = settings.access_token
    if settings.is_server() and settings.token_file is not None:
        token = token_store.load_token(settings.token_file) or token
        if os.name == "posix" and settings.token_file.exists():
            mode = stat.S_IMODE(settings.token_file.stat().st_mode)
            add(PASS if mode == 0o600 else FAIL, "Token file permissions", oct(mode))
    add(PASS if token else WARN, "Access token", "available" if token else "none yet: an admin generates it in Settings")

    try:
        db = Database(settings.db_path)
        db.get_setting("doctor_probe")
        add(PASS, "Database", str(settings.db_path))
        unresolved = len(db.unresolved_test_orders())
        add(PASS if not unresolved else FAIL, "Open test orders", f"{unresolved} unconfirmed")
        if db.get_setting(NEEDS_CHECK_SETTING):
            add(FAIL, "Order with no reply", "an order may exist; run the open-order check")
    except (OSError, sqlite3.Error) as exc:
        db = None
        add(FAIL, "Database", f"{settings.db_path}: {exc}")
    try:
        settings.log_path.parent.mkdir(parents=True, exist_ok=True)
        with settings.log_path.open("a", encoding="utf-8"):
            pass
        add(PASS, "Log file", str(settings.log_path))
    except OSError as exc:
        add(FAIL, "Log file", f"{settings.log_path}: {exc}")
    try:
        add(PASS, "Endpoint registry", f"{len(load_endpoints())} endpoints")
    except Exception as exc:  # any registry problem is a deployment failure
        add(FAIL, "Endpoint registry", str(exc))

    conn = connectivity_check(settings.base_url, 10.0)
    add(PASS if conn.get("ok") else FAIL, "TradeSmart API reachable",
        f"{conn.get('host')} ({conn.get('ip')})" if conn.get("ok") else str(conn.get("error")))

    check = network_guard.check_egress(settings, db)
    if check.matches:
        add(PASS, "Outbound IP matches static IP", check.detected or "")
    else:
        add(FAIL if settings.is_server() and check.expected else WARN, "Outbound IP vs static IP",
            f"detected {check.detected or 'unknown'}; {check.problem}")
    return results


def main() -> int:
    sys.stdout.reconfigure(errors="replace")  # Windows consoles can't print every character
    settings = load_settings()
    outbound.set_local_address(settings.outbound_bind_ip)
    results = run_checks(settings)
    for level, name, detail in results:
        print(f"[{level}] {name}" + (f": {detail}" if detail else ""))
    failed = sum(1 for level, _, _ in results if level == FAIL)
    print(f"\n{failed} failure(s)." if failed else "\nNo failures.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
