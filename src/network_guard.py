"""Static-IP guard: the server's real outbound IP must match the IP registered with TradeSmart.

The broker (Kambala/Noren OMS) identifies the caller by the network source address of each request. The API
has no field for it and this app sends no forwarding headers, so the only thing to verify is that this
server's traffic really leaves through the registered IP. Admins enter that IP in Settings; it overrides
``TS_STATIC_IP``. When enforcement is on (the default in server mode), orders are refused unless the IP is
set, can be detected and matches. Read-only requests only get a warning.
"""
from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from src import outbound

if TYPE_CHECKING:
    from src.database import Database
    from src.settings import Settings

logger = logging.getLogger("tsbench")

STATIC_IP_SETTING = "server_static_ip"
DETECT_TIMEOUT_S = 5.0


def normalize_ip(text: str) -> str:
    """Return ``text`` as a canonical IP address, or raise ``ValueError``."""
    return str(ipaddress.ip_address((text or "").strip()))


def is_public_ip(text: str) -> bool:
    try:
        return ipaddress.ip_address(text).is_global
    except ValueError:
        return False


def effective_static_ip(settings: "Settings", db: "Database | None" = None) -> str:
    """The expected public IP: the value saved in Settings, otherwise ``TS_STATIC_IP``."""
    saved = None
    if db is not None:
        try:
            saved = db.get_setting(STATIC_IP_SETTING)
        except Exception as exc:  # the guard then falls back to the env value
            logger.warning("Couldn't read the saved static IP: %s", exc)
    return str(saved or settings.static_ip or "").strip()


def save_static_ip(db: "Database", value: str, *, changed_by: str) -> str:
    """Validate and store the static IP (empty clears the saved value). Returns the stored value."""
    cleaned = normalize_ip(value) if (value or "").strip() else ""
    db.set_setting(STATIC_IP_SETTING, cleaned)
    logger.info("Static IP changed to %s by %s", cleaned or "(cleared)", changed_by)
    return cleaned


def detect_public_ip(url: str, *, client: httpx.Client | None = None, timeout: float = DETECT_TIMEOUT_S) -> str:
    """Ask an IP-echo service what address this server's traffic comes from. Raises on any failure."""
    own = client is None
    http = client or httpx.Client(timeout=timeout, **outbound.httpx_transport_kwargs())
    try:
        response = http.get(url, timeout=timeout)
        response.raise_for_status()
        return normalize_ip(response.text)
    finally:
        if own:
            http.close()


@dataclass(frozen=True)
class EgressCheck:
    expected: str
    detected: str | None
    enforced: bool
    error: str | None = None

    @property
    def matches(self) -> bool:
        return bool(self.expected and self.detected and self.expected == self.detected)

    @property
    def problem(self) -> str | None:
        """Plain-language description of what is wrong, or None when the IPs match."""
        if self.matches:
            return None
        if not self.expected:
            return "The server's static IP has not been set yet. An admin can enter it in Settings → Server IP."
        if self.error or not self.detected:
            return (f"Couldn't detect this server's public IP ({self.error or 'no answer'}), so it can't be "
                    f"compared with {self.expected}. Check that the server can reach the IP-check service.")
        return (f"This server's traffic leaves from {self.detected}, but the static IP is set to {self.expected}. "
                "TradeSmart would see the wrong address. Fix the server's routing or correct the IP in Settings.")

    @property
    def order_block_reason(self) -> str | None:
        """Why orders must be refused, or None. Only blocks when enforcement is on."""
        return self.problem if self.enforced else None


def check_egress(settings: "Settings", db: "Database | None" = None, *, client: httpx.Client | None = None) -> EgressCheck:
    """Compare the expected static IP with the detected outbound IP. Never raises."""
    expected = effective_static_ip(settings, db)
    try:
        detected, error = detect_public_ip(settings.ip_check_url, client=client), None
    except Exception as exc:  # reported on the result; the guard fails closed
        detected, error = None, f"{type(exc).__name__}: {exc}"
        logger.warning("Public IP check failed: %s", error)
    try:
        expected = normalize_ip(expected) if expected else ""
    except ValueError:
        logger.warning("The configured static IP is not a valid IP address")
    return EgressCheck(expected=expected, detected=detected, enforced=settings.enforce_static_ip, error=error)


def order_guard(settings: "Settings", db: "Database | None" = None) -> Any:
    """A zero-argument callable for ``run_order_test``: a fresh check, returning a block reason or None."""
    return lambda: check_egress(settings, db).order_block_reason
