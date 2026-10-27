"""Shared pytest fixtures. Tests must never reach the real TradeSmart API."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """Fail loudly if any test tries to open a real socket."""
    import socket

    def guard(*args, **kwargs):
        raise RuntimeError("Real network access is blocked in tests; use respx/MockTransport/fakes.")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "getaddrinfo", guard)
