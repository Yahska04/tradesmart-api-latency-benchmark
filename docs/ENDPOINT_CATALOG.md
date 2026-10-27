# TradeSmart API V2: Endpoint Catalog

- **Source of truth:** https://apidocs.tradesmartonline.in/v2/ (fetched 2026-10-16), plus the official Postman collection linked from that page (`assets/Tradesmart_v2.postman_collection.zip`).
- **Machine-readable registry:** `config/endpoints.json` (schema_version 1). This page is a readable summary of it.
- **Base URL:** `https://v2api.tradesmartonline.in/NorenWClientAPIv2`
- **WebSocket:** `wss://v2api.tradesmartonline.in/NorenWSAPI/`
- **Request format:** every call is `POST`. The body is raw text `jData=<JSON>`, and no Content-Type header is needed.
- **Auth:** send `Authorization: Bearer <access_token>` on every endpoint except `GenAcsTok`.
- **Response:** a JSON object or array. Check `stat` (`Ok` / `Not_Ok`) first. `emsg` comes back on failure. Business errors often arrive with HTTP 200.

The official API Reference lists **19 endpoints in 6 categories**. The Postman collection counts 26 requests because it includes the 8 PlaceOrder variants. The registry has 20 entries: the 19 documented endpoints plus `GetQuotesMultiple`, which the official site names only in its Rate Limits section.

## Catalog

Source confidence:
- **Official:** request fields, auth and response come from the endpoint's own reference page.
- **Official (partial):** the endpoint has a reference page, but the docs are internally inconsistent somewhere (see Notes).
- **Summary only:** request fields are not documented officially.

| Category | Endpoint | Method | Path | Auth | Required params | Safety class | Fixture needs | Rate limit | Source confidence |
|---|---|---|---|---|---|---|---|---|---|
| Authentication | User Details | POST | `/UserDetails` | Bearer | uid | read_only | none | none documented | Official |
| Authentication | Generate Access Token | POST | `/GenAcsTok` | none | code, checksum | special_setup (never auto) | fresh one-time OAuth code | none documented | Official |
| Orders | Place Order (8 variants) | POST | `/PlaceOrder` | Bearer | uid, actid, exch, tsym, qty, prc, dscqty, prd, trantype, prctyp, ret (+ trgprc for SL; blprc/bpprc/trailprc for CO/BO) | state_changing (disabled) | safe test env + confirmation | 30/s, 600/min | Official (partial) |
| Orders & Trades | Order Margin | POST | `/GetOrderMargin` | Bearer | uid, actid, exch, tsym, qty, prc, prd, trantype, prctyp | read_only (calculation only) | instrument + hypothetical order values | none documented | Official (partial) |
| Orders & Trades | Order Book | POST | `/OrderBook` | Bearer | uid, actid | read_only | none | none documented | Official (partial) |
| Orders & Trades | Order History | POST | `/SingleOrdHist` | Bearer | uid, norenordno | read_only (disabled by default) | existing norenordno | none documented | Official |
| Orders & Trades | Trade Book | POST | `/TradeBook` | Bearer | uid, actid | read_only | none | none documented | Official |
| Orders & Trades | Position Book | POST | `/PositionBook` | Bearer | uid, actid | read_only | none | none documented | Official |
| Orders & Trades | Exit Cover / Bracket Order | POST | `/ExitSNOOrder` | Bearer | uid, prd (H/B), norenordno | state_changing (disabled) | open CO/BO SNO leg | none documented | Official |
| Orders & Trades | Cancel Order | POST | `/CancelOrder` | Bearer | uid, norenordno | state_changing (disabled) | open order | none documented | Official |
| Orders & Trades | Modify Order | POST | `/ModifyOrder` | Bearer | uid, actid, exch, norenordno, tsym, qty, prc, prd, prctyp | state_changing (disabled) | open order | none documented | Official (partial) |
| Holdings & Limits | Holdings | POST | `/Holdings` | Bearer | uid, actid, prd (=C) | read_only | none | none documented | Official |
| Holdings & Limits | Limits | POST | `/Limits` | Bearer | uid, actid | read_only | none | none documented | Official |
| Scrips | Search Scrip | POST | `/SearchScrip` | Bearer | uid, stext | read_only | search text | none documented | Official |
| Scrips | Get Quotes | POST | `/GetQuotes` | Bearer | uid, exch, token | read_only | exch/token pair | 100/s, 1000/min | Official (partial) |
| Scrips | Get Quotes (Multiple) | POST | `/GetQuotesMultiple` | Bearer | uid (only field known) | read_only (disabled by default) | request body undocumented | 100/s, 1000/min | Summary only |
| Scrips | Security Info | POST | `/GetSecurityInfo` | Bearer | uid, exch, token | read_only | exch/token pair | none documented | Official |
| Scrips | Option Chain | POST | `/GetOptionChain` | Bearer | uid, tsym, exch, strprc, cnt | read_only | live F&O tsym + strike | none documented | Official (partial) |
| Market Info | Time Price Data | POST | `/TPSeries` | Bearer | uid, exch, token (st, et, intrv optional) | read_only | exch/token + time range | 100/s, 1000/min | Official |
| Market Info | Historical Data | POST | `/EODChartData` | Bearer | sym, from, to | read_only | sym + date range | none documented | Official |

The Rate Limits page states that **all other REST APIs have no rate limit**. The benchmark still applies its conservative default cap (`DEFAULT_UNDOCUMENTED_PER_SECOND`) to those endpoints.

### Placeholders used in `request_body_template`

| Placeholder | Fills field(s) |
|---|---|
| `{{ClientId}}` | uid, actid |
| `{{code}}`, `{{checksum}}` | GenAcsTok |
| `{{exch}}`, `{{token}}`, `{{tsym}}` | instrument identity |
| `{{searchtext}}` | SearchScrip `stext` |
| `{{strprc}}`, `{{cnt}}` | GetOptionChain |
| `{{st}}`, `{{et}}`, `{{intrv}}` | TPSeries |
| `{{sym}}`, `{{from}}`, `{{to}}` | EODChartData |
| `{{norenordno}}` | SingleOrdHist, CancelOrder, ModifyOrder, ExitSNOOrder |
| `{{qty}}`, `{{prc}}`, `{{prd}}`, `{{trantype}}`, `{{prctyp}}`, `{{ret}}`, `{{dscqty}}` | order and margin fields |
| `{{scrip_keys}}`, `{{access_token}}` | WebSocket messages only |

The Holdings `prd` field is fixed to the only documented value, `"C"`. The `ordersource` field on PlaceOrder is fixed to `"API"`.

### Response notes

- **OrderBook, SingleOrdHist, TPSeries:** return a JSON **array** when data exists.
- **"No data" responses:** TradeBook, PositionBook, SingleOrdHist, GetQuotes and TPSeries return `{"stat":"Not_Ok","emsg":"Error Occurred : 5 \"no data\""}` with HTTP 200 when there is no data. Treat this as an empty dataset, not a failure.
- **EODChartData:** returns an array of JSON-encoded candle strings, or `[]` when empty.
- **SearchScrip:** the docs' failure sample is a 404 with `{"stat":"Not_Ok","emsg":"No Data : "}`.

## WebSocket protocol (official `#websocket`)

**Connection rules**
- All messages are JSON.
- Send the connect message as soon as the socket opens.
- Send a heartbeat every **30 s**.
- Maximum **1,000 subscriptions** per user or connection.
- Scrip keys use the format `EXCH|TOKEN`, joined with `#` (for example `NSE|22#BSE|508123`).

| Purpose | Send | Ack | Feed | Unsubscribe → ack |
|---|---|---|---|---|
| Connect | `{"t":"a","uid","actid","source":"API","accesstoken"}` | `ak` (`s`: Ok/Not_Ok) | – | – |
| Touchline | `{"t":"t","k":"..."}` | `tk` (one per scrip) | `tf` | `{"t":"u","k"}` → `uk` |
| Depth | `{"t":"d","k":"..."}` | `dk` (one per scrip, 5-level book) | `df` (changed fields only) | `{"t":"ud","k"}` → `udk` |
| Order updates | `{"t":"o","actid"}` | **none** | `om` | `{"t":"uo"}` → `uok` |
| Position updates | `{"t":"p","uid"}` | `pk` | `pm` | `{"t":"up"}` → `upk` |
| Heartbeat | `{"t":"h"}` | `hk` (with `ft`) | – | – |

## Script Master downloads

`https://v2api.tradesmartonline.in/<EXCH>_symbols.txt.zip` for NSE, NFO, BSE, BFO, BCD, CDS and MCX. Each zip holds one `.txt` file. Cache it locally rather than downloading it per request.

## Gaps / needs user input

**What you must supply before read-only benchmarking**
1. **Credentials and identity:** `TS_CLIENT_ID` (for `{{ClientId}}`) and a valid `TS_ACCESS_TOKEN`. Getting the token needs a one-time OAuth code, the app key and the secret key, all through GenAcsTok. This step is manual and is never benchmarked.
2. **Instrument for GetQuotes, GetSecurityInfo and TPSeries:** an `exch` / `token` pair, for example `NSE` plus a numeric token from the NSE Script Master. The docs sample for GetSecurityInfo uses NSE / `11536`.
3. **Search term for SearchScrip:** a value for `{{searchtext}}`, such as an instrument name.
4. **Inputs for GetOptionChain:** a current, non-expired option or future `tsym`, its `exch` (NFO / BFO / CDS / MCX), a mid `strprc` and a `cnt`.
5. **Time range for TPSeries:** epoch-second `st` / `et` and an `intrv` (1, 3, 5, 10, 15, 30, 60, 120 or 240). The range must fit the history window: NSE/BSE 360 days, MCX 70, NFO/BFO/CDS/BCD 65.
6. **Inputs for EODChartData:** `sym` as `EXCH:TSYM` (for example `NSE:SBIN-EQ`) and an epoch-second `from` / `to` range inside the same history window.
7. **Hypothetical order for GetOrderMargin:** `exch`, `tsym`, `qty`, `prc`, `prd`, `trantype` and `prctyp`. This endpoint only calculates margin and places nothing.
8. **Order number for SingleOrdHist:** an existing `norenordno` from this account. The endpoint is disabled by default until you provide one.
9. **WebSocket instruments:** scrip keys (`EXCH|TOKEN`) for touchline and depth tests.

**What the official docs do not specify, or contradict**
- **GetQuotesMultiple:** named only in the Rate Limits section. It has no reference page and is missing from the Postman collection, so its request fields are not confirmed from the official docs; verify them before use. Only `uid` is templated, and the endpoint is disabled by default.
- **PlaceMultiOrder:** named only in the Rate Limits section (30/s, 600/min shared with PlaceOrder), with no reference page. It is **not registered** as an endpoint, and it stays in `NEVER_AUTO_EXECUTE`.
- **Path differences from the local summary:** the official paths are `/GetOrderMargin` (the summary says `/OrderMargin`) and `/ExitSNOOrder` (the summary says `/ExitSnoOrder`). The registry uses the official paths.
- **OrderBook:** the field table marks `actid` as Required, but the example payload sends only `uid`. The template sends both.
- **GetQuotes and TPSeries:** the `token` field is described as a "contract token", but the samples send symbols (`TCS-EQ`, `INFY`). Use numeric tokens from Script Master.
- **GetOptionChain:** the inline example payload (`token`, no `exch`) contradicts the field table. The Request Body sample and Postman use `tsym`/`exch`/`strprc`/`cnt`, and the template follows those.
- **GetOrderMargin and ModifyOrder:** the samples send fields that are not in the field tables (`rorgqty`/`rorgprc` and `dscqty`/`trantype` respectively). The templates include only the fields from the tables.
- **PlaceOrder `amo`:** the field table marks it "Required", but its description says to send it only for AMO orders. It is not in the template.
- **Holdings:** no example responses are published, so the empty-account response shape is unknown.
- **Token lifetime:** the GenAcsTok sample response shows `expires_in` and `refresh_token`, but neither appears in the documented field table, and no refresh endpoint is documented.
- **UAT / sandbox:** no UAT or sandbox base URL is documented. State-changing endpoints therefore stay registered for documentation only, with manual latency entry.
