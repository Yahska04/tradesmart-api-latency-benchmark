"""Command-line open-order check: ``python -m src.recover_orders``.

Runs before the web app starts on a server (``ExecStartPre`` in ``deploy/tsbench.service``), so test orders left
open by a crash or power cut are cancelled without waiting for someone to open the page. It never cancels an
order that isn't one of the app's own tagged test orders. Exit code: 0 clean or nothing to do, 1 needs attention.
"""
from __future__ import annotations

import sys

from src import outbound, token_store
from src.api_client import TradeSmartClient
from src.database import Database
from src.endpoint_registry import load_endpoints
from src.order_test import NEEDS_CHECK_SETTING, OrderTestError, reconcile_test_orders
from src.redaction import register_secrets, setup_logging
from src.settings import load_settings


def main() -> int:
    sys.stdout.reconfigure(errors="replace")  # Windows consoles can't print every character
    settings = load_settings()
    logger = setup_logging(settings.log_path, settings.secret_values())
    outbound.set_local_address(settings.outbound_bind_ip)
    token = settings.access_token
    if settings.is_server() and settings.token_file is not None:
        token = token_store.load_token(settings.token_file) or token
    db = Database(settings.db_path)
    unresolved = db.unresolved_test_orders()
    needs_check = bool(db.get_setting(NEEDS_CHECK_SETTING))
    if not token or not settings.client_id:
        if unresolved or needs_check:
            print(f"ATTENTION: {len(unresolved)} test order(s) unconfirmed, but no access token is available. "
                  "Check the TradeSmart order book.")
            logger.warning("Open-order check skipped: no token, %d order(s) unconfirmed", len(unresolved))
            return 1
        print("No access token yet; no unconfirmed test orders.")
        return 0
    register_secrets([token])
    endpoints = {ep.id: ep for ep in load_endpoints()}
    with TradeSmartClient(settings.base_url, access_token=token, timeout_s=10.0, secrets=[token]) as client:
        try:
            report = reconcile_test_orders(db, endpoints, client, client_id=settings.client_id)
        except OrderTestError as exc:
            print(f"Skipped: {exc}")
            return 1 if unresolved or needs_check else 0
    print(f"Open-order check: {report.checked} checked, {len(report.cancelled)} cancelled, "
          f"{len(report.closed)} already closed, {len(report.filled)} FILLED, {len(report.still_open)} still open.")
    for line in report.errors:
        print(f"  {line}")
    for number in report.filled + report.still_open:
        print(f"  ATTENTION: check order {number} in TradeSmart.")
    return 1 if report.needs_attention else 0


if __name__ == "__main__":
    sys.exit(main())
