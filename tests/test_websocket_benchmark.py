"""Tests for src.websocket_benchmark using a scripted fake connection (no real network)."""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager

import pytest

from src.websocket_benchmark import (
    METRIC_LABEL,
    WSBenchmarkConfig,
    WSBenchmarkResult,
    run_ws_benchmark,
    run_ws_benchmark_sync,
)

TOKEN = "WsSecretToken999"


@pytest.fixture(scope="module")
def loop():
    """Event loop created before the per-test socket guard (the Windows proactor needs a socketpair)."""
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


class FakeWS:
    """Scripted connection: replies to auth/subscribe and then streams feed updates."""

    def __init__(self, feed_count: int = 5, interval_s: float = 0.005, ak_status: str = "OK",
                 die_after: int | None = None, feed: str = "t") -> None:
        self.sent: list[dict] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.feed_count = feed_count
        self.interval_s = interval_s
        self.ak_status = ak_status
        self.die_after = die_after
        self.feed = feed
        self.closed = False
        self._task: asyncio.Task | None = None

    async def send(self, text: str) -> None:
        msg = json.loads(text)
        self.sent.append(msg)
        if msg["t"] == "a":
            await self.queue.put({"t": "ak", "s": self.ak_status})
        elif msg["t"] in ("t", "d"):
            await self.queue.put({"t": "tk" if msg["t"] == "t" else "dk", "e": "NSE", "tk": "22", "lp": "100.0"})
            self._task = asyncio.ensure_future(self._stream())

    async def _stream(self) -> None:
        upd = "tf" if self.feed == "t" else "df"
        for i in range(self.feed_count):
            await asyncio.sleep(self.interval_s)
            if self.die_after is not None and i >= self.die_after:
                await self.queue.put(ConnectionError("connection reset by peer"))
                return
            await self.queue.put({"t": upd, "e": "NSE", "tk": "22", "lp": str(100 + i)})

    async def recv(self) -> str:
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    async def close(self) -> None:
        self.closed = True
        if self._task:
            self._task.cancel()


def make_connect(conns: list[FakeWS], factory):
    @asynccontextmanager
    async def connect(url: str):
        ws = factory(len(conns))
        conns.append(ws)
        try:
            yield ws
        finally:
            await ws.close()

    return connect


def cfg(**kw) -> WSBenchmarkConfig:
    base = dict(ws_url="wss://mock.tradesmart.test/NorenWSAPI/", client_id="AB1234", access_token=TOKEN,
                instruments=["NSE|22", "BSE|508123"], duration_s=0.15, heartbeat_s=10.0, max_reconnects=2,
                connect_timeout_s=1.0)
    base.update(kw)
    return WSBenchmarkConfig(**base)


def test_touchline_flow(loop):
    conns: list[FakeWS] = []
    res = loop.run_until_complete(run_ws_benchmark(cfg(), connect=make_connect(conns, lambda i: FakeWS(5))))
    ws = conns[0]
    assert ws.sent[0] == {"t": "a", "uid": "AB1234", "actid": "AB1234", "source": "API", "accesstoken": TOKEN}
    assert ws.sent[1] == {"t": "t", "k": "NSE|22#BSE|508123"}
    assert ws.sent[-1]["t"] == "u" and ws.closed
    for value in (res.connect_ms, res.auth_ack_ms, res.subscribe_ack_ms, res.first_message_ms):
        assert value is not None and value >= 0
    assert res.message_counts["ak"] == 1 and res.message_counts["tk"] == 1 and res.message_counts["tf"] == 5
    assert len(res.inter_arrival_ms) == 4 and all(v > 0 for v in res.inter_arrival_ms)
    assert res.disconnects == 0 and res.reconnects == 0 and res.errors == []
    assert res.started_at.endswith("Z") and res.completed_at.endswith("Z")
    s = res.summary()
    assert s["label"] == METRIC_LABEL and "NOT exchange" in s["label"]
    assert s["interval_count"] == 4 and s["interval_p50_ms"] is not None
    assert s["total_messages"] == 7 and any("P95" in w for w in s["warnings"])


def test_depth_feed(loop):
    conns: list[FakeWS] = []
    res = loop.run_until_complete(run_ws_benchmark(cfg(feed="d"),
                                                   connect=make_connect(conns, lambda i: FakeWS(3, feed="d"))))
    assert conns[0].sent[1]["t"] == "d" and res.message_counts["df"] == 3 and res.subscribe_ack_ms is not None


def test_heartbeat_sent(loop):
    conns: list[FakeWS] = []
    res = loop.run_until_complete(run_ws_benchmark(cfg(heartbeat_s=0.04, duration_s=0.15),
                                                   connect=make_connect(conns, lambda i: FakeWS(0))))
    beats = [m for m in conns[0].sent if m == {"t": "h"}]
    assert len(beats) >= 2 and res.message_counts.get("h_sent") == len(beats)


def test_reconnect_with_backoff(loop):
    conns: list[FakeWS] = []

    def factory(i):
        return FakeWS(5, die_after=2) if i == 0 else FakeWS(3)

    res = loop.run_until_complete(run_ws_benchmark(cfg(duration_s=0.2), connect=make_connect(conns, factory),
                                                   backoff_base_s=0.01))
    assert len(conns) == 2 and res.disconnects == 1 and res.reconnects == 1
    assert any("connection reset" in e for e in res.errors)
    assert len(res.connect_samples_ms) == 2
    # intervals are not computed across the reconnect gap: (2-1) + (3-1)
    assert len(res.inter_arrival_ms) == 3


def test_gives_up_after_max_reconnects(loop):
    attempts = []

    @asynccontextmanager
    async def failing_connect(url):
        attempts.append(url)
        raise OSError(f"refused for token {TOKEN}")
        yield  # pragma: no cover

    res = loop.run_until_complete(run_ws_benchmark(cfg(max_reconnects=2, duration_s=1.0), connect=failing_connect,
                                                   backoff_base_s=0.001))
    assert len(attempts) == 3 and res.reconnects == 2 and res.disconnects == 0
    assert res.connect_ms is None and any("giving up" in e for e in res.errors)
    assert all(TOKEN not in e for e in res.errors)


def test_auth_rejected_does_not_reconnect(loop):
    conns: list[FakeWS] = []
    res = loop.run_until_complete(run_ws_benchmark(cfg(), connect=make_connect(conns,
                                                                               lambda i: FakeWS(ak_status="NOT_OK"))))
    assert len(conns) == 1 and res.auth_ack_ms is None and any("rejected" in e for e in res.errors)
    assert len(conns[0].sent) == 1  # no subscribe after rejection


def test_stop_event(loop):
    stop = threading.Event()
    stop.set()
    res = loop.run_until_complete(run_ws_benchmark(cfg(duration_s=5), connect=make_connect([], lambda i: FakeWS()),
                                                   stop_event=stop))
    assert res.connect_ms is None and res.duration_s < 1


def test_config_validation_and_masking():
    assert cfg().validate() == []
    bad = cfg(ws_url="http://x", instruments=["22"], feed="x", client_id="")
    assert len(bad.validate()) >= 3
    d = cfg(duration_s=999).to_dict()
    assert TOKEN not in json.dumps(d) and "AB1234" not in json.dumps(d) and d["duration_s"] == 300


def test_summary_empty():
    res = WSBenchmarkResult("ws-1", None, None, None, None, [], {}, 0, 0, [], "", "")
    s = res.summary()
    assert s["interval_p50_ms"] is None and s["warnings"]


class FakeDB:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def create_run(self, **kw):
        self.calls.append(("create_run", kw))
        return kw["run_id"]

    def insert_ws_samples(self, run_id, samples):
        self.calls.append(("insert_ws_samples", run_id, list(samples)))

    def finish_run(self, run_id, status, stop_reason=None):
        self.calls.append(("finish_run", run_id, status, stop_reason))


def test_sync_persists(loop):
    db = FakeDB()
    res = run_ws_benchmark_sync(cfg(), db=db, loop=loop, run_id="ws-test-1",
                                connect=make_connect([], lambda i: FakeWS(4)))
    names = [c[0] for c in db.calls]
    assert names == ["create_run", "insert_ws_samples", "finish_run"]
    create = db.calls[0][1]
    assert create["run_type"] == "websocket" and create["run_id"] == "ws-test-1"
    assert TOKEN not in json.dumps(create["config"])
    metrics = {s[0] for s in db.calls[1][2]}
    assert {"connect_ms", "auth_ack_ms", "subscribe_ack_ms", "first_message_ms", "inter_arrival_ms"} <= metrics
    assert db.calls[2][2] == "completed" and res.run_id == "ws-test-1"
