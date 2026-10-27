# TradeSmart API Latency Benchmark

A locally hosted tool that measures **client-observed** response latency for TradeSmart API V2
endpoints. It stores every request sample in SQLite and reports actual P50, P95 and P99 per endpoint.

It is built for internal performance analysis and debugging. **Benchmarks and endpoint checks never place,
modify or cancel orders.** The only exception is the opt-in **Order latency test** page (see below). It places tiny,
guarded BUY LIMIT orders on your live account and cancels each one immediately, and you must type a confirmation for every batch.

| Layer | Technology |
|---|---|
| UI | Streamlit + Plotly (TradeSmart brand theme) |
| HTTP + timing | HTTPX, `time.perf_counter_ns()` |
| Stats | pandas, NumPy (`numpy.percentile(..., method="linear")`) |
| Storage | SQLite (`data/benchmark.db`) |
| WebSocket | `websockets` (separate module and metrics) |
| Tests | pytest, respx / `httpx.MockTransport` (no real network in tests) |

---

## What gets measured

- **Client-observed latency**: the time from just before the request is sent to just after the full
  response body is read. It includes DNS, TCP and TLS (cold mode), network transit, server processing
  and response transfer. It is **not** pure server processing time.
- **Cold vs warm**: *cold* opens a new connection for every request; *warm* reuses one keep-alive
  connection.
- **Success** means HTTP 2xx **and** `stat == "Ok"` (or a JSON list, or `Not_Ok` with "no data").
  HTTP 200 + `stat: Not_Ok` counts as a failure (`api_error`). Timeouts and connection errors are
  recorded as failures with their elapsed time and are never dropped.
- **Percentiles** use successful, non-warm-up samples only. The sample count is always shown.
  P95 needs at least 20 samples and P99 at least 100 to be called reliable. Below that, the app shows a
  warning.

See the **Methodology** page in the app for the full explanation.

## Safety model

Each endpoint in `config/endpoints.json` has a safety class:

| Class | Examples | Automated benchmark? |
|---|---|---|
| `read_only` | UserDetails, Limits, Holdings, OrderBook, GetQuotes, TPSeries | Yes (rate-limited) |
| `state_changing` | PlaceOrder, ModifyOrder, CancelOrder, ExitSNOOrder | **Never** by benchmarks or checks. PlaceOrder/CancelOrder only via the guarded Order latency test page |
| `special_setup` | GenAcsTok (one-time OAuth code), fixtures such as SingleOrdHist | Only with explicit setup |

Order endpoint names are hard-blocked in code (`src/safety.py`) for the benchmark runner and the Check page,
even if the JSON is edited. `src/order_test.py` is the only code path that sends PlaceOrder/CancelOrder.
Requests are paced with a safety factor (default 50%) below documented rate limits. Endpoints without a
documented limit get a conservative cap. A `429` stops the run, and repeated failures pause the endpoint.
Nothing is retried automatically.

---

## Windows setup (VS Code + PowerShell)

Open the project folder in VS Code, then open a terminal with **Terminal → New Terminal** (PowerShell).

### 1. Check Python and install dependencies

```powershell
python --version          # 3.12 or 3.13
cd "D:\AI Bootcamp\Projects\Latency"
```

### 2. Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# If activation is blocked:  Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
python -m pip install --upgrade pip
pip install -r requirements.txt
```

In VS Code, choose **Python: Select Interpreter** → `.venv`.

### 3. Configure `.env`

```powershell
Copy-Item .env.example .env
code .env
```

Fill in `TS_APP_KEY`, `TS_SECRET_KEY`, `TS_CLIENT_ID` and `TS_REDIRECT_URL` from your TradeSmart API app.
Either paste a current `TS_ACCESS_TOKEN` or generate one in the app (**Settings → OAuth helper**).
Tokens generated in the app are kept in the browser session only. They are never written to disk.
`.env` is git-ignored. Secrets are masked in the UI, logs and CSV exports.

### 4. Review the endpoint registry

Open `config/endpoints.json` (see `docs/ENDPOINT_CATALOG.md` for a readable version) or use the
**Endpoint Registry** page. Instrument-specific endpoints need values such as exchange and token
(for example `NSE` / `22`). You pick them from an instrument search on the **Run Benchmark** and
**Check an endpoint** pages (see *Script master* below), or type them by hand.

### 5. Run the automated tests

```powershell
python -m pytest -q
```

Tests use mocked HTTP and a socket guard, so they never contact TradeSmart.

### 6. Start the app

```powershell
streamlit run app.py        # or: python app.py   or double-click run.bat
```

**Script master.** On start-up the app downloads TradeSmart's public instrument lists (NSE, NFO, BSE,
BFO, BCD, CDS, MCX — URLs in `config/endpoints.json` → `script_master`) in the background, at most once
per IST day, into `data/scriptmaster/instruments.db` (next to `TS_DB_PATH`). No credentials are sent.
The sidebar shows its status. Pages that need `{{exch}}`, `{{token}}`, `{{tsym}}` or `{{sym}}` then offer
an instrument search (e.g. `SBIN`, `NIFTY 25000 CE`); **Enter values manually** still lets you type
values, which override the picked instrument. **Settings → Script master** shows per-exchange status
and a **Refresh script master** button. A failed exchange keeps its previous list. Set
`TS_SCRIPT_MASTER_AUTOSTART=0` to skip the start-up download (offline use).

### 7. Open the dashboard

Streamlit opens <http://localhost:8501> automatically. If it doesn't, open that URL in a browser.
No credentials yet? Click **Load demo data** on the Overview page. Demo data is labelled
**SYNTHETIC DEMO** everywhere and is kept out of real aggregates.

### 7b. Check a single endpoint (quick test)

Use **Benchmark → Check an endpoint**, or **Check this endpoint** on an endpoint's detail tab in the registry:

1. Pick one read-only endpoint. Order and other state-changing endpoints are not listed and can't be sent.
2. Fill in its request values. Your client ID is prefilled from `.env` and masked. You can also edit the
   JSON body; keep `{{ClientId}}` as a placeholder.
3. Choose 1–20 requests, the delay and the connection mode, then click **Send requests**.
4. See each request's latency, HTTP status, `stat`/`emsg`, size and server timing, plus a **redacted** preview
   of the last response body. The preview is never saved, logged or exported.
5. Optionally tick **Save these samples to history** to keep them as a normal run in Analytics and History.

This is a spot check. Use **Run benchmark** for P95/P99 you can rely on.

### 7c. Measure order latency (opt-in, places REAL orders)

Use **Benchmark → Order latency test**. Enter the batch **once**, for example 100 orders. The app then runs it
automatically in bursts:

1. It fetches one fresh quote at the start of each burst.
2. It places up to **8 orders back to back** (in about one second). TradeSmart's limit is 10 orders per second.
3. It **cancels every order of the burst** and reads each final status back with SingleOrdHist.
4. It waits your **delay between bursts** (default 3 s, adjustable from 1 to 60 s), counted from the burst's last
   order, then starts the next burst.

Every order is:

- BUY · LIMIT · DAY · quantity 1, on NSE/BSE cash equities only. It is never a market order. Pick the share
  with the search box; the token is filled in from the script master.
- Priced 1–10% (default 3%) below the live quote, rounded down to the tick size and never below the lower circuit.
  The quote's symbol must match the share you picked, or nothing is sent.
- Capped at ₹5,000 in value. At most **1,000 orders per trading day (IST)**, counted from saved results.

Placements and cancellations together stay at or under 8 per rolling 1.25 s. The extra 0.25 s absorbs network
jitter, so TradeSmart never sees more than 8 in any second. Orders stay open for about 1–2 s before they are
cancelled. You type `PLACE n ORDERS` once for the whole batch. Nothing is retried. **Stop the batch** stops
placing new orders; orders already placed in that burst are still cancelled. If an order is rejected, gets no
reply, fails to cancel, is filled or shows an unexpected status, every order of that burst is still cancelled.
The batch then stops with a red alert naming the order to check. As a guide, 100 orders in bursts of 8 with a
3 s delay take about a minute. 1,000 orders take about 8–9 minutes. Results are saved as a normal run and appear
in Performance analytics.

### 8. Run a small benchmark

1. **Settings**: confirm the environment and that an access token is present.
2. **Run Benchmark**: select a few read-only endpoints (for example `UserDetails`, `Limits`).
3. Set the iterations to 20, warm-up to 2 and delay to 500 ms. Use warm connections.
4. Optionally click **Check connectivity**.
5. Review the plan, including the planned request count and estimated duration, then click **Start benchmark**.

### 9. Review P50 / P95 / P99

Open **Performance Analytics** to see the endpoint table, distribution charts, comparison and
request-level samples. Open **Historical Comparison** to compare runs.

### 10. Export to CSV

**Performance analytics → Request log → Download requests CSV** (and **Download metrics CSV** for the aggregated P50/P95/P99 table).
Exports are whitelisted and redacted. They contain no tokens, account IDs or response bodies.

---

## Project layout

```text
app.py                      Overview dashboard
views/                      Pages: Check, Registry, Run, Analytics, Historical, Settings, Methodology, WebSocket
                            (not named pages/ on purpose: navigation is defined explicitly in app.py)
src/settings.py             .env / environment settings (secrets masked)
src/redaction.py            secret masking + logging filter
src/endpoint_registry.py    endpoint definitions and payload templates
src/safety.py               safety classes, guards, rate limiter, circuit breaker
src/auth.py                 OAuth URL, SHA-256 checksum, GenAcsTok
src/api_client.py           HTTPX client, jData encoding, timed requests
src/benchmark_engine.py     plan + sequential runner (optional low concurrency)
src/metrics.py              percentiles, summaries, run comparison
src/database.py             SQLite persistence
src/exporters.py            CSV export
src/demo_data.py            synthetic demo data
src/websocket_benchmark.py  WebSocket connect/ack/message-interval benchmark
src/script_master.py        instrument lists (script master): download, SQLite store, search
src/ui/                     theme, components, state helpers
config/endpoints.json       endpoint registry
docs/ARCHITECTURE.md        module contract
docs/ENDPOINT_CATALOG.md    endpoint catalog + documentation gaps
tests/                      pytest suite
scripts/convert_logos.py    converts official logo artwork into assets/
```

## Known limitations

- Latency is client-observed. Your network, location, VPN and Wi-Fi all affect results.
- Results vary with market session (IST 09:15–15:30), server load and time of day. Compare runs only under
  similar conditions. The app flags differences in configuration.
- TradeSmart's static-IP requirement, if any applies to your account, is not bypassed. Run the tool from
  the registered network.
- WebSocket metrics are message-arrival intervals observed by the client. They are not
  exchange-to-client latency.
- Order endpoints support documentation-only registration and manual result entry until a safe sandbox
  is available.
