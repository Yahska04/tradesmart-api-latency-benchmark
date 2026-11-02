"""Source address for outgoing connections (optional, for servers with several network interfaces).

All TradeSmart traffic is made by this Python process, so the broker sees the server's public IP whichever
device opened the dashboard. On a server with one route out, nothing here is needed. On a server with several
interfaces, ``TS_OUTBOUND_BIND_IP`` pins REST, WebSocket and IP-check traffic to one local address.
"""
from __future__ import annotations

from typing import Any

_local_address: str | None = None


def set_local_address(address: str | None) -> None:
    """Set the local address outgoing connections bind to; empty/None means the OS routing table decides."""
    global _local_address
    _local_address = (address or "").strip() or None


def local_address() -> str | None:
    return _local_address


def httpx_transport_kwargs(verify: Any = True) -> dict[str, Any]:
    """Keyword arguments for ``httpx.Client``: a bound transport when configured, otherwise just ``verify``."""
    if not _local_address:
        return {"verify": verify}
    import httpx

    return {"transport": httpx.HTTPTransport(verify=verify, local_address=_local_address)}


def websocket_kwargs() -> dict[str, Any]:
    """Keyword arguments for ``websockets.connect`` (passed through to the asyncio connection)."""
    return {"local_addr": (_local_address, 0)} if _local_address else {}
