# TradeSmart API V2 Documentation

> Developer reference for integrating trading, portfolio, market-data and WebSocket functionality with TradeSmart API V2.
>
> Source: https://apidocs.tradesmartonline.in/v2/
>
> Documentation checked: 1 October 2026

---

## 1. Overview

TradeSmart API V2 provides REST APIs and WebSocket streaming for building trading and investment applications.

### Core capabilities

- OAuth authentication and access-token management
- User/account details
- Order placement, modification and cancellation
- Cover and Bracket Order support
- Order book, trade book and order history
- Positions, holdings and limits
- Scrip search, quotes and option chain
- Intraday/time-price-series data
- Historical chart data
- Live market-data streaming
- Live order and position updates
- Exchange-wise instrument/script masters

### Supported exchanges / segments

| Exchange code | Segment |
|---|---|
| `NSE` | NSE Equity |
| `BSE` | BSE Equity |
| `NFO` | NSE Futures & Options |
| `BFO` | BSE Futures & Options |
| `CDS` | Currency derivatives |
| `BCD` | BSE Currency |
| `MCX` | Commodity |

---

# 2. Base URLs

## REST API

```text
https://v2api.tradesmartonline.in/NorenWClientAPIv2
```

## WebSocket

```text
wss://v2api.tradesmartonline.in/NorenWSAPI/
```

All REST calls use HTTPS.

REST requests use:

```text
jData={...}
```

The JSON object is sent as the request body with the `jData=` prefix.

---

# 3. Authentication

TradeSmart API V2 uses OAuth-based authentication.

## Authentication flow

```text
OAuth Login
    |
    v
Authorization Code
    |
    v
GenAcsTok
    |
    v
Access Token
    |
    v
Authenticated REST APIs
    |
    +----> WebSocket
```

## Required credentials

An application receives:

- App Key
- Secret Key
- Client ID
- Redirect URL

### Security

The Secret Key must remain on a server controlled by the application.

Do not expose the Secret Key in:

- Browser JavaScript
- Mobile applications
- Public repositories
- Front-end source code

---

# 4. OAuth Login

## Authorization URL

```http
GET https://v2api.tradesmartonline.in/OAuthlogin/authorize/oauth?client_id=YOUR_APP_KEY
```

The user logs in with their TradeSmart credentials.

TradeSmart redirects the user to the registered Redirect URL with a one-time authorization code.

Example:

```text
https://yourdomain.com/callback?code=AUTHORIZATION_CODE
```

The authorization code is valid for a single token exchange.

---

# 5. Generate Access Token

## Endpoint

```http
POST {{BaseUrl}}/GenAcsTok
```

Authentication:

```text
None
```

## Checksum

Create a SHA-256 checksum using:

```text
App Key + Secret Key + Authorization Code
```

No spaces or separators are used.

### Example

```python
import hashlib

checksum = hashlib.sha256(
    (APP_KEY + SECRET_KEY + AUTH_CODE).encode()
).hexdigest()
```

## Request

```json
{
  "code": "<authorization_code>",
  "checksum": "<sha256_hex_digest>"
}
```

## Example

```python
import hashlib
import json
import requests

BASE_URL = "https://v2api.tradesmartonline.in/NorenWClientAPIv2"

APP_KEY = "YOUR_APP_KEY"
SECRET_KEY = "YOUR_SECRET_KEY"
AUTH_CODE = "CODE_FROM_REDIRECT_URL"

checksum = hashlib.sha256(
    (APP_KEY + SECRET_KEY + AUTH_CODE).encode()
).hexdigest()

response = requests.post(
    f"{BASE_URL}/GenAcsTok",
    data="jData=" + json.dumps({
        "code": AUTH_CODE,
        "checksum": checksum
    }),
    timeout=10
)

data = response.json()
access_token = data["access_token"]
```

The returned access token is required for authenticated API calls.

---

# 6. Authentication Header

All authenticated REST APIs require:

```http
Authorization: Bearer YOUR_ACCESS_TOKEN
```

`GenAcsTok` is the exception and does not require a Bearer token.

---

# 7. User Details

## Endpoint

```http
POST {{BaseUrl}}/UserDetails
```

## Request

```json
{
  "uid": "{{ClientId}}"
}
```

## Purpose

Use this endpoint after authentication to verify connectivity and retrieve account metadata.

The response contains information such as:

- Enabled exchanges
- Enabled product types
- Account information

Use the returned exchange/product values when constructing order requests.

---

# 8. Common Request Structure

Every REST API call is an HTTP POST.

Example:

```http
POST {{BaseUrl}}/UserDetails
Authorization: Bearer {{access_token}}
```

Body:

```text
jData={
    "uid": "{{ClientId}}"
}
```

### Important

The request body is sent in `jData=` format.

---

# 9. Common Response Structure

API responses are JSON.

The first field to check is:

```json
{
  "stat": "Ok"
}
```

or:

```json
{
  "stat": "Not_Ok",
  "emsg": "Error description"
}
```

### Common fields

| Field | Description |
|---|---|
| `stat` | `Ok` or `Not_Ok` |
| `request_time` | Server/request timestamp |
| `emsg` | Error description when request fails |

Always check `stat` before processing endpoint-specific response fields.

---

# 10. API Reference

## Authentication

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/GenAcsTok` | Generate access token |
| POST | `/UserDetails` | Retrieve user/account details |

## Orders

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/PlaceOrder` | Place an order |

## Orders & Trades

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/OrderMargin` | Calculate/order margin |
| POST | `/OrderBook` | Retrieve order book |
| POST | `/SingleOrdHist` | Retrieve order history |
| POST | `/TradeBook` | Retrieve trade book |
| POST | `/PositionBook` | Retrieve positions |
| POST | `/ExitSnoOrder` | Exit Cover / Bracket Order |
| POST | `/CancelOrder` | Cancel an order |
| POST | `/ModifyOrder` | Modify an order |

## Holdings & Limits

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/Holdings` | Retrieve holdings |
| POST | `/Limits` | Retrieve funds/limits |

## Scrips / Market Information

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/SearchScrip` | Search instruments |
| POST | `/GetQuotes` | Get quote |
| POST | `/GetQuotesMultiple` | Get multiple quotes |
| POST | `/GetSecurityInfo` | Get security information |
| POST | `/GetOptionChain` | Get option chain |

## Market Data

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/TPSeries` | Time-price series |
| POST | `/EODChartData` | Historical candle data |

> Endpoint names should always be taken from the latest official API documentation before production implementation.

---

# 11. Place Order

## Endpoint

```http
POST {{BaseUrl}}/PlaceOrder
```

Authentication:

```http
Authorization: Bearer {{access_token}}
```

The endpoint is used to place trading orders.

Typical order parameters include:

- `uid`
- `actid`
- `exch`
- `tsym`
- `qty`
- `prc`
- `prd`
- `trantype`
- `prctyp`
- `ret`

The exact required fields depend on the order variant and product.

## Transaction type

| Value | Meaning |
|---|---|
| `B` | Buy |
| `S` | Sell |

## Price type

| Value | Meaning |
|---|---|
| `LMT` | Limit |
| `MKT` | Market |
| `SL-LMT` | Stop-loss Limit |
| `SL-MKT` | Stop-loss Market |

## Important response

Successful order submission returns a `norenordno`.

```json
{
  "stat": "Ok",
  "norenordno": "250000000000123"
}
```

The order number represents acceptance by the OMS. It should not be interpreted as confirmation that the order has been executed.

Use Order Book, Single Order History or WebSocket order updates to determine the final order state.

---

# 12. Order Status

Common order statuses:

| Status | Meaning |
|---|---|
| `PENDING` | Order accepted and awaiting further exchange events |
| `OPEN` | Order is open/working |
| `TRIGGER_PENDING` | Waiting for trigger |
| `COMPLETE` | Fully executed |
| `CANCELED` | Cancelled |
| `REJECTED` | Rejected by risk/exchange |

For real-time status, prefer WebSocket order updates.

---

# 13. Modify Order

## Endpoint

```http
POST {{BaseUrl}}/ModifyOrder
```

Required fields include:

```json
{
  "uid": "{{ClientId}}",
  "actid": "{{ClientId}}",
  "exch": "NSE",
  "norenordno": "ORDER_NUMBER"
}
```

Additional modification fields depend on the order type.

A successful modify request indicates that the request was accepted by the OMS. Confirm the resulting order state through Order Book, Single Order History or WebSocket updates.

---

# 14. Cancel Order

## Endpoint

```http
POST {{BaseUrl}}/CancelOrder
```

Typical identifier:

```json
{
  "uid": "{{ClientId}}",
  "norenordno": "ORDER_NUMBER"
}
```

A cancellation acknowledgement does not by itself guarantee that the order has reached its final exchange state.

Confirm the final state through:

- Order Book
- Single Order History
- WebSocket order updates

---

# 15. Order Book

## Endpoint

```http
POST {{BaseUrl}}/OrderBook
```

Use this endpoint to retrieve the client's current order information.

It can be used to verify:

- Order status
- Order number
- Exchange
- Trading symbol
- Quantity
- Price
- Product
- Transaction type
- Rejection information

---

# 16. Single Order History

## Endpoint

```http
POST {{BaseUrl}}/SingleOrdHist
```

Use this endpoint when the complete lifecycle/history of a specific order is required.

This is particularly useful after:

- Modify
- Cancel
- Exchange rejection
- Partial execution
- Other order-state transitions

---

# 17. Trade Book

## Endpoint

```http
POST {{BaseUrl}}/TradeBook
```

Use this endpoint to retrieve executed trades.

Typical use cases:

- Reconciliation
- Trade reporting
- P&L processing
- Strategy execution tracking

---

# 18. Position Book

## Endpoint

```http
POST {{BaseUrl}}/PositionBook
```

Use this endpoint to retrieve current positions.

Position data can be used for:

- Strategy monitoring
- Risk management
- Portfolio dashboards
- Position reconciliation

---

# 19. Holdings

## Endpoint

```http
POST {{BaseUrl}}/Holdings
```

Returns holdings for the trading account.

---

# 20. Limits / Funds

## Endpoint

```http
POST {{BaseUrl}}/Limits
```

Use this endpoint to retrieve available funds and account limits.

Before placing an order, applications may use this information to validate available margin/funds.

---

# 21. Search Scrip

## Endpoint

```http
POST {{BaseUrl}}/SearchScrip
```

Use this endpoint to find:

- Exchange
- Trading symbol
- Token
- Instrument details

For automated systems, the Script Master should generally be cached locally instead of repeatedly searching/downloading static data.

---

# 22. Quotes

## Endpoint

```http
POST {{BaseUrl}}/GetQuotes
```

Use this endpoint to retrieve quote information for an instrument.

For multiple instruments, use:

```http
POST {{BaseUrl}}/GetQuotesMultiple
```

For continuous live market data, WebSocket streaming is preferred over repeatedly polling the quote APIs.

---

# 23. Option Chain

## Endpoint

```http
POST {{BaseUrl}}/GetOptionChain
```

Use this API to retrieve option-chain information for supported instruments.

Typical applications:

- Strike discovery
- Expiry selection
- Option strategy builders
- Market analysis

---

# 24. Security Information

## Endpoint

```http
POST {{BaseUrl}}/GetSecurityInfo
```

Use this endpoint to retrieve security/instrument information required by an application.

---

# 25. Time-Price Series

## Endpoint

```http
POST {{BaseUrl}}/TPSeries
```

Use this endpoint to retrieve time-price/candle data.

The API supports fields such as:

- Open
- High
- Low
- Close
- Volume
- Timestamp

---

# 26. Historical Data

## Endpoint

```http
POST {{BaseUrl}}/EODChartData
```

This API provides historical candle data.

Example:

```json
{
  "sym": "BSE:SBIN",
  "from": "1690934400",
  "to": "1691107200"
}
```

The response provides historical candle information including timestamp and OHLC/volume data.

---

# 27. Cover / Bracket Orders

TradeSmart API V2 supports Cover / Bracket order functionality through the relevant order endpoints.

Use the endpoint-specific documentation for the exact request parameters and supported variants.

For production integrations, do not assume product codes or order fields across segments. Validate against the values returned by User Details and the relevant endpoint documentation.

---

# 28. WebSocket Streaming

## URL

```text
wss://v2api.tradesmartonline.in/NorenWSAPI/
```

WebSocket can provide:

- Touchline/market feed
- Market depth
- Order updates
- Position updates
- Heartbeat

## Connection flow

```text
Open WebSocket
      |
      v
Connect: t=a
      |
      v
Subscribe to feeds
      |
      +----> Touchline: t=t
      |
      +----> Depth: t=d
      |
      +----> Orders: t=o
      |
      +----> Positions: t=p
      |
      v
Heartbeat every 30 seconds
```

---

# 29. WebSocket Connect

Immediately after opening the socket:

```json
{
  "t": "a",
  "uid": "{{ClientId}}",
  "actid": "{{ClientId}}",
  "source": "API",
  "accesstoken": "{{access_token}}"
}
```

Successful connection acknowledgement:

```text
t = ak
```

---

# 30. WebSocket Touchline

## Subscribe

```json
{
  "t": "t",
  "k": "NSE|22#BSE|508123"
}
```

The `k` field contains exchange/token pairs.

Example:

```text
NSE|22
BSE|508123
```

Common touchline fields include:

- LTP
- Percentage change
- Volume
- Open
- High
- Low
- Close
- Average price
- Open interest
- Best bid/ask
- Feed timestamp

Feed updates use:

```text
t = tf
```

## Unsubscribe

```json
{
  "t": "u",
  "k": "NSE|22#BSE|508123"
}
```

---

# 31. WebSocket Market Depth

## Subscribe

```json
{
  "t": "d",
  "k": "NSE|22#BSE|508123"
}
```

Depth provides a 5-level order book.

Typical fields:

```text
bq1-bq5   Buy quantities
bp1-bp5   Buy prices
bo1-bo5   Buy order counts

sq1-sq5   Sell quantities
sp1-sp5   Sell prices
so1-so5   Sell order counts
```

Depth feed updates use:

```text
t = df
```

---

# 32. WebSocket Order Updates

## Subscribe

```json
{
  "t": "o",
  "actid": "{{ClientId}}"
}
```

There is no separate acknowledgement for order-update subscription.

Order updates are received using:

```text
t = om
```

Use this feed for near-real-time order lifecycle updates.

---

# 33. WebSocket Position Updates

Position updates use the position subscription/feed mechanism documented by TradeSmart.

Task types include:

```text
p  - position subscription/request
pk - acknowledgement
pm - position update
```

---

# 34. WebSocket Heartbeat

Send a heartbeat every 30 seconds:

```json
{
  "t": "h"
}
```

Applications should avoid repeatedly opening and closing WebSocket connections.

Use reconnect logic with backoff when a connection is lost.

---

# 35. Rate Limits

Current documented limits:

| API | Per second | Per minute |
|---|---:|---:|
| Place Order / Place Multi Order | 30 | 600 |
| TPSeries | 100 | 1000 |
| GetQuotes / GetQuotesMultiple | 100 | 1000 |
| Other REST APIs | No documented rate limit | No documented rate limit |

HTTP `429` indicates that the application is sending too many requests.

Recommended response:

1. Reduce request rate.
2. Reduce concurrency.
3. Apply backoff.
4. Use WebSocket for continuous live market data instead of polling.

---

# 36. Script Master

Tradeable instrument masters are available by exchange/segment.

| Exchange | File |
|---|---|
| NSE | `NSE_symbols.txt.zip` |
| NFO | `NFO_symbols.txt.zip` |
| BSE | `BSE_symbols.txt.zip` |
| BFO | `BFO_symbols.txt.zip` |
| BCD | `BCD_symbols.txt.zip` |
| CDS | `CDS_symbols.txt.zip` |
| MCX | `MCX_symbols.txt.zip` |

Recommended implementation:

```text
Download Script Master
        |
        v
Extract TXT
        |
        v
Store locally
        |
        v
Index by exchange/token/symbol
        |
        v
Use local lookup during trading
```

Avoid downloading the master on every API request.

---

# 37. Error Handling

## Application-level errors

A business/API failure can return HTTP 200 with:

```json
{
  "stat": "Not_Ok",
  "emsg": "Invalid Access Token"
}
```

Therefore:

> Never use HTTP 200 alone as the success condition.

Always check:

```text
HTTP status
    +
stat
    +
emsg when stat = Not_Ok
```

## HTTP status codes

| Code | Meaning |
|---|---|
| `200` | Response returned - still check `stat` |
| `400` | Bad request |
| `401` | Unauthorized |
| `403` | Forbidden |
| `404` | Endpoint/path not found |
| `429` | Too many requests |
| `5xx` | Server/gateway error |

---

# 38. Retry Strategy

Do not retry all failures blindly.

### Business errors

Examples:

- Insufficient funds
- Invalid symbol
- Invalid quantity
- Risk rejection
- Exchange rejection

These should generally be handled by correcting the request.

### Transient errors

Examples:

- Network timeout
- Gateway error
- HTTP 5xx
- Temporary connectivity failure

These can be retried using exponential backoff.

Example strategy:

```text
Attempt 1 -> immediate
Attempt 2 -> 250 ms
Attempt 3 -> 500 ms
Attempt 4 -> 1 sec
Attempt 5 -> 2 sec
```

The actual retry policy should also respect API rate limits and the application's trading-risk requirements.

---

# 39. Order State Handling

An order API acknowledgement is not the same as final execution status.

Recommended architecture:

```text
Place Order
     |
     v
OMS acknowledgement
     |
     +----> Store norenordno
     |
     v
WebSocket order update
     |
     v
Final order state
```

For critical reconciliation:

```text
WebSocket
    +
Order Book
    +
Single Order History
    +
Trade Book
```

Use these sources together when required to establish the final state of an order.

---

# 40. Suggested Integration Architecture

```text
                    +----------------------+
                    | Trading Application  |
                    +----------+-----------+
                               |
                +--------------+--------------+
                |                             |
                v                             v
        REST API Client                WebSocket Client
                |                             |
        +-------+--------+          +---------+---------+
        |                |          |                   |
        v                v          v                   v
     Orders          Portfolio   Market Data       Order Updates
        |                |          |                   |
        +----------------+----------+-------------------+
                               |
                               v
                     Application Database
                               |
                               v
                    Strategy / Risk Engine
```

---

# 41. Recommended API Client Design

A production API client should separate:

### Authentication layer

```text
OAuth login
Authorization code
Checksum
Access token
Token lifecycle
```

### Trading layer

```text
Place order
Modify order
Cancel order
Order history
Trade book
Position book
```

### Market-data layer

```text
Search scrip
Quotes
Option chain
TPSeries
Historical data
WebSocket
```

### Portfolio layer

```text
Holdings
Limits
Positions
Orders
Trades
```

### Reliability layer

```text
Timeouts
Retries
Rate limiting
Logging
Idempotency handling
Connection recovery
Reconciliation
```

---

# 42. Python Quickstart

```python
import hashlib
import json
import requests

BASE_URL = "https://v2api.tradesmartonline.in/NorenWClientAPIv2"

APP_KEY = "YOUR_APP_KEY"
SECRET_KEY = "YOUR_SECRET_KEY"
CLIENT_ID = "YOUR_CLIENT_ID"
AUTH_CODE = "CODE_FROM_REDIRECT_URL"


def post(endpoint, payload, token=None):
    headers = {}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.post(
        f"{BASE_URL}/{endpoint}",
        data="jData=" + json.dumps(payload),
        headers=headers,
        timeout=10
    )

    response.raise_for_status()

    data = response.json()

    if isinstance(data, dict) and data.get("stat") == "Not_Ok":
        raise RuntimeError(
            f"{endpoint} failed: {data.get('emsg')}"
        )

    return data


# 1. Generate checksum
checksum = hashlib.sha256(
    (APP_KEY + SECRET_KEY + AUTH_CODE).encode()
).hexdigest()


# 2. Generate access token
token_response = post(
    "GenAcsTok",
    {
        "code": AUTH_CODE,
        "checksum": checksum
    }
)

access_token = token_response["access_token"]


# 3. Verify authentication
user = post(
    "UserDetails",
    {
        "uid": CLIENT_ID
    },
    access_token
)

print(json.dumps(user, indent=2))
```

---

# 43. Node.js Request Pattern

```javascript
const BASE_URL =
  "https://v2api.tradesmartonline.in/NorenWClientAPIv2";

async function apiRequest(endpoint, payload, accessToken) {
  const response = await fetch(`${BASE_URL}/${endpoint}`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${accessToken}`
    },
    body: `jData=${JSON.stringify(payload)}`
  });

  const data = await response.json();

  if (data.stat === "Not_Ok") {
    throw new Error(data.emsg || "TradeSmart API request failed");
  }

  return data;
}
```

---

# 44. Integration Checklist

## Application setup

- [ ] TradeSmart API access enabled
- [ ] Application registered
- [ ] App Key received
- [ ] Secret Key received
- [ ] Client ID available
- [ ] Redirect URL registered
- [ ] Credentials stored securely

## Authentication

- [ ] OAuth login implemented
- [ ] Authorization code captured
- [ ] SHA-256 checksum implemented
- [ ] Access token generation implemented
- [ ] Bearer token added to API requests

## Trading

- [ ] User Details verified
- [ ] Script Master integrated
- [ ] Order placement implemented
- [ ] Modify order implemented
- [ ] Cancel order implemented
- [ ] Order status handling implemented
- [ ] Trade reconciliation implemented

## Market data

- [ ] Scrip lookup implemented
- [ ] Quotes implemented
- [ ] Option chain implemented
- [ ] Historical data implemented
- [ ] WebSocket implemented
- [ ] Heartbeat implemented
- [ ] Reconnection implemented

## Reliability

- [ ] Timeout handling
- [ ] HTTP error handling
- [ ] `stat` validation
- [ ] `emsg` logging
- [ ] Rate-limit handling
- [ ] Exponential backoff
- [ ] WebSocket reconnect
- [ ] Order reconciliation

## Security

- [ ] Secret Key never exposed client-side
- [ ] Credentials stored securely
- [ ] Access tokens protected
- [ ] Redirect URL controlled by the application
- [ ] Production logs do not expose credentials/tokens

---

# 45. Important Implementation Notes

1. `HTTP 200` does not necessarily mean a successful API operation. Always check `stat`.
2. A successful order acknowledgement means the request was accepted by the OMS, not necessarily executed by the exchange.
3. Use WebSocket for real-time market/order/position updates instead of aggressive REST polling.
4. Cache Script Master data locally.
5. Respect documented limits for Place Order, TPSeries and Quotes.
6. Do not expose the Secret Key in browser or mobile application code.
7. Use Order Book / Single Order History / Trade Book for reconciliation.
8. Use WebSocket order updates for near-real-time order state.
9. Product codes and supported values can depend on the account, exchange segment and risk configuration.
10. Validate production behavior against the current official documentation before deploying live trading logic.

---

# 46. Official Documentation

TradeSmart API V2:

https://apidocs.tradesmartonline.in/v2/

REST Base URL:

https://v2api.tradesmartonline.in/NorenWClientAPIv2

WebSocket:

wss://v2api.tradesmartonline.in/NorenWSAPI/

---

## Document Maintenance

This file is a developer-oriented summary of the official TradeSmart API V2 documentation.

When the official documentation changes, update this file's:

- Authentication section
- Endpoint catalogue
- Request/response fields
- Rate limits
- WebSocket protocol
- Order states
- Changelog
- Supported exchanges
