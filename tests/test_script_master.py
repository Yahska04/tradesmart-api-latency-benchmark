"""Script master: parse, store, refresh (skip/force/failure), search ranking, lookups, guards, status.

All downloads are served by ``httpx.MockTransport`` from in-memory zips that mimic the real files.
"""
from __future__ import annotations

import io
import threading
import zipfile
from datetime import datetime, timezone

import httpx
import pytest

from src import script_master as sm

# Headers exactly as observed in TradeSmart's files (2026-10), including the trailing comma.
NSE_TXT = (
    "Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,\n"
    "NSE,26000,1,Nifty 50,NIFTY INDEX,INDEX,0,\n"
    "NSE,3045,1,SBIN,SBIN-EQ,EQ,0.05,\n"
    "NSE,24524,1,SBINEQWETF,SBINEQWETF-EQ,EQ,0.01,\n"
    "NSE,2885,1,RELIANCE,RELIANCE-EQ,EQ,0.10,\n"
    "NSE,99999,1,ABCSBIN,ABCSBIN-EQ,EQ,0.05,\n"
    "NSE,,1,BROKEN,,EQ,0.05,\n"
)
BSE_TXT = (
    "Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,\n"
    "BSE,500112,1,SBIN,SBIN,A,0.05,\n"
)
NFO_TXT = (
    "Exchange,Token,LotSize,Symbol,TradingSymbol,Expiry,Instrument,OptionType,StrikePrice,TickSize,\n"
    "NFO,61471,65,NIFTY,NIFTY23NOV26F,23-NOV-2026,FUTIDX,XX,-0.01,0.1,\n"
    "NFO,71503,65,NIFTY,NIFTY29DEC26C25000,29-DEC-2026,OPTIDX,CE,25000,0.05,\n"
    "NFO,71504,65,NIFTY,NIFTY29DEC26P25000,29-DEC-2026,OPTIDX,PE,25000,0.05,\n"
    "NFO,64439,65,NIFTY,NIFTY23NOV26C25000,23-NOV-2026,OPTIDX,CE,25000,0.05,\n"
    "NFO,64500,65,NIFTY,NIFTY23NOV26C25500,23-NOV-2026,OPTIDX,CE,25500,0.05,\n"
    "NFO,48994,750,SBIN,SBIN27OCT26F,27-OCT-2026,FUTSTK,XX,-0.01,0.05,\n"
)
# BFO spells the strike column "Strike"; MCX adds unknown columns that must be ignored.
BFO_TXT = (
    "Exchange,Token,LotSize,Symbol,TradingSymbol,Expiry,Instrument,OptionType,Strike,TickSize,\n"
    "BFO,1134712,2425,ZOMAOPT,ETERNAL26OCT370PE,29-OCT-2026,OPTSTK,PE,370,0.05,\n"
)
MCX_TXT = (
    "Exchange,Token,LotSize,GNGD,Symbol,TradingSymbol,Expiry,Instrument,OptionType,StrikePrice,TickSize,"
    "PriceUnits,TradeUnits,DeliveryUnits,\n"
    "MCX,587579,5,100,SUNOIL,SUNOIL31DEC26,31-DEC-2026,FUTCOM,XX,0,0.1,KGS,MT,KGS,\n"
)

NOW = datetime(2026, 10, 22, 3, 30, tzinfo=timezone.utc)          # 09:00 IST, 22 Oct
LATER_SAME_IST_DAY = datetime(2026, 10, 22, 17, 0, tzinfo=timezone.utc)   # 22:30 IST, 22 Oct
NEXT_IST_DAY = datetime(2026, 10, 22, 19, 0, tzinfo=timezone.utc)         # 00:30 IST, 23 Oct


def make_zip(text: str, name: str = "NSE_symbols.txt", extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, text)
        for n, data in (extra or {}).items():
            zf.writestr(n, data)
    return buf.getvalue()


class Server:
    """MockTransport handler serving one zip per exchange; records requests."""

    def __init__(self, files: dict[str, bytes | int]) -> None:
        self.files = files
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        exch = request.url.path.strip("/").split("_")[0]
        item = self.files.get(exch, 404)
        if isinstance(item, int):
            return httpx.Response(item, content=b"nope")
        return httpx.Response(200, content=item, headers={"content-type": "application/zip"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def hits(self, exch: str) -> int:
        return sum(1 for r in self.requests if r.url.path.startswith(f"/{exch}_"))


@pytest.fixture(autouse=True)
def storage(tmp_path):
    sm.set_storage_dir(tmp_path / "scriptmaster")
    yield tmp_path / "scriptmaster"
    sm.set_storage_dir(None)


@pytest.fixture
def loaded():
    server = Server({"NSE": make_zip(NSE_TXT), "BSE": make_zip(BSE_TXT, "BSE_symbols.txt"),
                     "NFO": make_zip(NFO_TXT, "NFO_symbols.txt")})
    with server.client() as client:
        res = sm.refresh(["NSE", "BSE", "NFO"], client=client, now=NOW)
    assert all(r["ok"] for r in res.values()), res
    return server


# ============================================================================ parsing
def test_sources_come_from_registry():
    srcs = sm.sources()
    assert {"NSE", "NFO", "BSE", "BFO", "BCD", "CDS", "MCX"} <= set(srcs)
    assert srcs["NSE"]["url"].endswith("/NSE_symbols.txt.zip")


def test_parse_equity_header():
    rows = sm.parse_symbols_text(NSE_TXT, "nse")
    assert len(rows) == 5  # row without token/tsym skipped
    sbin = next(r for r in rows if r.tsym == "SBIN-EQ")
    assert sbin == sm.Instrument(exch="NSE", token="3045", tsym="SBIN-EQ", symbol="SBIN", instrument="EQ",
                                 lot_size=1, tick_size=0.05)
    assert sbin.label == "NSE · SBIN-EQ (3045)"
    assert not sbin.is_derivative


def test_parse_derivative_headers_and_aliases():
    nfo = {r.tsym: r for r in sm.parse_symbols_text(NFO_TXT, "NFO")}
    fut = nfo["NIFTY23NOV26F"]
    assert (fut.expiry, fut.option_type, fut.strike, fut.lot_size) == ("23-NOV-2026", None, None, 65)
    ce = nfo["NIFTY29DEC26C25000"]
    assert (ce.option_type, ce.strike, ce.instrument, ce.tick_size) == ("CE", 25000.0, "OPTIDX", 0.05)
    bfo = sm.parse_symbols_text(BFO_TXT, "BFO")[0]
    assert (bfo.option_type, bfo.strike) == ("PE", 370.0)  # "Strike" column alias
    mcx = sm.parse_symbols_text(MCX_TXT, "MCX")[0]
    assert (mcx.tsym, mcx.symbol, mcx.lot_size, mcx.tick_size) == ("SUNOIL31DEC26", "SUNOIL", 5, 0.1)


def test_parse_is_case_insensitive_and_rejects_unknown_header():
    rows = sm.parse_symbols_text("EXCHANGE,TOKEN,TRADINGSYMBOL\r\nNSE,1,ABC-EQ\r\n", "NSE")
    assert rows[0].tsym == "ABC-EQ" and rows[0].symbol == "ABC-EQ"
    with pytest.raises(sm.ScriptMasterError, match="unexpected header"):
        sm.parse_symbols_text("Foo,Bar\n1,2\n", "NSE")


def test_extract_text_only_reads_txt_members():
    content = make_zip(NSE_TXT, extra={"readme.exe": b"MZ" * 10, "dir/notes.csv": b"x"})
    assert sm.extract_text(content) == NSE_TXT
    with pytest.raises(sm.ScriptMasterError, match="no .txt"):
        sm.extract_text(make_zip("x", name="data.csv"))
    with pytest.raises(sm.ScriptMasterError, match="not a valid zip"):
        sm.extract_text(b"<html>not a zip</html>")


def test_zip_size_guard():
    bomb = make_zip(NSE_TXT + "NSE,1,1,X,X-EQ,EQ,0.05,\n" * 5000)
    assert len(bomb) < 20_000
    with pytest.raises(sm.ScriptMasterError, match="exceeds"):
        sm.extract_text(bomb, max_uncompressed=10_000)


# ============================================================================ refresh
def test_refresh_stores_and_sends_no_credentials(loaded):
    assert sm.instruments_count(["NSE"]) == 5
    assert sm.instruments_count() == 5 + 1 + 6
    for req in loaded.requests:
        assert req.method == "GET"
        assert "authorization" not in req.headers and "cookie" not in req.headers
        assert not req.url.query


def test_refresh_skips_same_ist_day_and_force_redownloads(loaded):
    with loaded.client() as client:
        res = sm.refresh(["NSE"], client=client, now=LATER_SAME_IST_DAY)
        assert res["NSE"]["skipped"] and res["NSE"]["ok"] and res["NSE"]["rows"] == 5
        assert loaded.hits("NSE") == 1
        res = sm.refresh(["NSE"], client=client, now=LATER_SAME_IST_DAY, force=True)
        assert not res["NSE"]["skipped"] and loaded.hits("NSE") == 2
        res = sm.refresh(["NSE"], client=client, now=NEXT_IST_DAY)
        assert not res["NSE"]["skipped"] and loaded.hits("NSE") == 3


def test_failed_exchange_keeps_old_data_and_others_continue(loaded):
    server = Server({"NSE": 503, "BSE": make_zip("garbage,header\n1,2\n"), "NFO": b"not a zip",
                     "MCX": make_zip(MCX_TXT, "MCX_symbols.txt")})
    with server.client() as client:
        res = sm.refresh(["NSE", "BSE", "NFO", "MCX"], client=client, now=NEXT_IST_DAY)
    assert not res["NSE"]["ok"] and "HTTP 503" in res["NSE"]["error"]
    assert not res["BSE"]["ok"] and "unexpected header" in res["BSE"]["error"]
    assert not res["NFO"]["ok"] and "zip" in res["NFO"]["error"]
    assert res["MCX"]["ok"] and res["MCX"]["rows"] == 1
    # previous rows are still there
    assert sm.lookup("NSE", "SBIN-EQ") is not None
    assert sm.instruments_count(["NFO"]) == 6
    st = sm.status()
    assert st["state"] == "partial"
    nse = st["per_exchange"]["NSE"]
    assert nse["ok"] is False and nse["rows"] == 5 and "503" in nse["error"]
    assert nse["downloaded_at"] == "2026-10-22T03:30:00Z"  # last good download kept
    # a failure is retried on the next refresh (not skipped as "done today")
    with server.client() as client:
        assert not sm.refresh(["NSE"], client=client, now=NEXT_IST_DAY)["NSE"]["skipped"]


def test_network_error_is_reported_not_raised():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with httpx.Client(transport=httpx.MockTransport(boom)) as client:
        res = sm.refresh(["NSE", "XYZ"], client=client, now=NOW)
    assert not res["NSE"]["ok"] and "network error" in res["NSE"]["error"]
    assert not res["XYZ"]["ok"] and "not listed" in res["XYZ"]["error"]
    assert sm.status()["state"] == "error"


def test_zip_guard_during_refresh_keeps_old_data(loaded, monkeypatch):
    monkeypatch.setattr(sm, "MAX_UNCOMPRESSED_BYTES", 100)
    server = Server({"NSE": make_zip(NSE_TXT)})
    with server.client() as client:
        res = sm.refresh(["NSE"], client=client, now=NOW, force=True)
    assert not res["NSE"]["ok"] and "exceeds" in res["NSE"]["error"]
    assert sm.instruments_count(["NSE"]) == 5


def test_download_size_guard(monkeypatch):
    monkeypatch.setattr(sm, "MAX_DOWNLOAD_BYTES", 50)
    server = Server({"NSE": make_zip(NSE_TXT)})
    with server.client() as client:
        res = sm.refresh(["NSE"], client=client, now=NOW)
    assert not res["NSE"]["ok"] and "download exceeds" in res["NSE"]["error"]


def test_replacing_an_exchange_is_complete(loaded):
    server = Server({"NSE": make_zip("Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,\n"
                                     "NSE,1,1,NEWCO,NEWCO-EQ,EQ,0.05,\n")})
    with server.client() as client:
        sm.refresh(["NSE"], client=client, now=NOW, force=True)
    assert sm.instruments_count(["NSE"]) == 1
    assert sm.lookup("NSE", "SBIN-EQ") is None
    assert sm.lookup("BSE", "SBIN") is not None  # other exchanges untouched


# ============================================================================ status / background
def test_status_idle_then_ready():
    st = sm.status()
    assert st["state"] == "idle" and st["updated_at"] is None
    assert set(st["per_exchange"]) >= {"NSE", "NFO"}
    srcs = sm.sources()
    files = {e: make_zip(NSE_TXT.replace("NSE,", f"{e},"), f"{e}_symbols.txt") for e in srcs}
    server = Server(files)
    with server.client() as client:
        sm.refresh(client=client, now=NOW)
    st = sm.status()
    assert st["state"] == "ready" and st["exchanges_ready"] == len(srcs)
    assert st["updated_at"] == "2026-10-22T03:30:00Z"
    assert sm.to_ist(st["updated_at"]).strftime("%H:%M") == "09:00"


def test_start_background_refresh_is_idempotent(monkeypatch):
    gate = threading.Event()
    calls: list[tuple] = []

    def fake_refresh(exchanges=None, *, force=False, **kw):
        calls.append((exchanges, force))
        gate.wait(5)
        return {}

    monkeypatch.setattr(sm, "refresh", fake_refresh)
    sm.start_background_refresh(["NSE"])
    sm.start_background_refresh(["BSE"], force=True)  # no-op while the first is running
    assert sm.is_refreshing()
    assert sm.status()["state"] == "downloading"
    gate.set()
    sm._thread.join(5)
    assert calls == [(["NSE"], False)]
    sm._set_progress(running=False)
    assert not sm.is_refreshing()


# ============================================================================ search / lookup
def test_search_ranking_exact_then_prefix_then_contains(loaded):
    labels = [i.tsym for i in sm.search("sbin")]
    # exact symbol (NSE, then BSE, then NFO future), prefix, then contains
    assert labels[:3] == ["SBIN-EQ", "SBIN", "SBIN27OCT26F"]
    assert labels.index("SBINEQWETF-EQ") < labels.index("ABCSBIN-EQ")
    assert labels[-1] == "ABCSBIN-EQ"


def test_search_exchange_filter_and_limit(loaded):
    assert [i.exch for i in sm.search("SBIN", ["NSE", "BSE"])] == ["NSE", "BSE", "NSE", "NSE"]
    assert len(sm.search("SBIN", limit=2)) == 2
    assert sm.search("   ") == [] and sm.search("zzz") == []


def test_search_multi_word_derivatives(loaded):
    res = sm.search("NIFTY 25000 CE")
    assert [i.tsym for i in res] == ["NIFTY23NOV26C25000", "NIFTY29DEC26C25000"]  # nearest expiry first
    assert all(i.option_type == "CE" and i.strike == 25000 for i in res)
    assert [i.tsym for i in sm.search("nifty fut")] == ["NIFTY23NOV26F"]
    first = sm.search("NIFTY")[0]
    assert first.tsym == "NIFTY23NOV26F"  # futures before options


def test_lookup_and_by_token(loaded):
    inst = sm.lookup("nse", "sbin-eq")
    assert inst is not None and inst.token == "3045"
    assert sm.by_token("BSE", "500112").tsym == "SBIN"
    assert sm.by_token("NSE", "500112") is None
    assert sm.lookup("NSE", "NOPE") is None
    assert sm.has_data(["NSE"]) and not sm.has_data(["MCX"])


def test_instrument_values_for_placeholders():
    from src.ui.instrument_picker import instrument_values, option_label

    inst = sm.parse_symbols_text(NSE_TXT, "NSE")[1]
    assert instrument_values(inst) == {"exch": "NSE", "token": "3045", "tsym": "SBIN-EQ", "sym": "NSE:SBIN-EQ"}
    assert instrument_values(inst, ["sym"]) == {"sym": "NSE:SBIN-EQ"}
    assert instrument_values(None) == {}
    ce = sm.parse_symbols_text(NFO_TXT, "NFO")[1]
    assert option_label(ce) == "NFO · NIFTY29DEC26C25000 · token 71503 · exp 29-DEC-2026 · lot 65"


def test_status_pending_when_some_exchanges_not_fetched_yet(loaded):
    # NSE/BSE/NFO succeeded; the remaining registry exchanges were never attempted -> not a failure
    st = sm.status()
    assert st["state"] == "pending"
    assert st["exchanges_ready"] == 3 and all(p["ok"] is not False for p in st["per_exchange"].values())
