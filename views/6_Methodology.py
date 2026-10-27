"""Methodology: how latency is measured and how to read the numbers."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from src.ui.theme import apply_theme  # noqa: E402

apply_theme("How we measure latency",
            subtitle="What the numbers mean, and what they don't.")

SECTIONS: list[tuple[str, str]] = [
    ("What we time: the client's view", """
Every number here is **client-observed latency**: the time from just before your computer sends a
request until the full response has arrived. We time it with a monotonic, high-resolution clock
(`time.perf_counter_ns()`).

That time includes your network, the internet path, TLS, TradeSmart's servers and the trip back.
It is **not** pure server processing time. If the API sends server-timing headers, we store them
separately and never treat them as the full server time.
"""),
    ("Cold vs warm connections", """
- **Warm (connection reuse on):** one keep-alive connection is reused. Samples show the request
  itself, without connection setup. Good for comparing endpoints.
- **Cold (connection reuse off):** every request opens a new connection, so each sample includes
  DNS lookup, TCP connect and the TLS handshake. Closer to a first call from a fresh client.

Cold numbers are higher by design. Only compare runs that used the same mode.
"""),
    ("P50, P95 and P99 in plain words", """
- **P50 (median):** half the requests were faster than this.
- **P95:** 95 in 100 requests were faster. 1 in 20 was slower.
- **P99:** 99 in 100 were faster. This is the "bad day" tail.

**Exact method.** Percentiles come from successful, non-warm-up requests only, using
`numpy.percentile(values, q, method="linear")` — linear interpolation between the two closest
ranked samples. Overall percentiles pool every request in the selection, so the mix of endpoints
changes them; use per-endpoint numbers for like-for-like reading.
"""),
    ("Small samples, shaky tails", """
A tail percentile needs enough data to mean anything:

- **P95 needs at least 20 samples.**
- **P99 needs at least 100 samples.**

Below that, the dashboard still shows the number but flags it as *indicative only* and shows the
sample count beside it. With 30 samples, "P99" is really just the slowest request or two.
"""),
    ("The network moves under you", """
Wi-Fi vs cable, VPNs, ISP routing, other traffic on your line and the distance to TradeSmart's
servers all change results. Run comparisons from the same place, on the same network, with the
same settings. Run the connectivity check before a benchmark to see DNS, TCP and TLS times on
their own.
"""),
    ("Market hours change server load", """
Indian equity markets trade **09:15–15:30 IST** (pre-open from 09:00). Broker systems are busiest
at the open, around big news and near the close. A run at 09:16 and a run at 20:00 measure
different conditions. Timestamps are stored in UTC and shown in IST so you can line runs up with
the session.
"""),
    ("Authentication overhead", """
Each request carries your access token in the `Authorization: Bearer` header, and the server
validates it. That cost is part of every sample. Getting the token (`GenAcsTok`) is a one-time
login step and is **never** included in benchmarks.
"""),
    ("HTTP 200 can still be a failure", """
TradeSmart answers many errors with **HTTP 200** and `"stat": "Not_Ok"` in the body. We read the
body, so:

- 2xx + `stat: Ok` (or a valid list) → **success**
- 2xx + `stat: Not_Ok` → **API error** (counted as a failure)
- non-2xx → **HTTP error**; 429 → **rate limited**
- no response in time → **timeout**; DNS or connection problems are labelled as such

Failures and timeouts are never dropped and never count as fast requests. They are excluded from
percentiles and reported next to them, so a good P95 can't hide a bad success rate.
"""),
    ("\"No data\" is still an answer", """
Some endpoints return `Not_Ok` with a message like *"no data"* when, say, you hold no positions.
The server did the work and replied, so we count it as a **completed request** and keep its
latency. Its response is small, so compare it with care against endpoints that return real data.
"""),
    ("Why two runs never match exactly", """
Different time of day, market activity, network path, connection mode, iteration count, delay,
server deployments and plain randomness. The Historical comparison page lists any recorded
setting that differs between two runs, and the endpoint comparison refuses to rank endpoints
measured under different conditions.
"""),
    ("Rate limits and the safety factor", """
We follow the documented limits (for example, quotes: 100 per second and 1,000 per minute) and
run **below** them. The safety factor sets how much of the limit we use — 0.5 means half. Endpoints
without a documented limit are capped at a conservative 5 requests per second. Requests run one at
a time by default, with a delay between them. A 429 response stops the run; repeated failures
pause that endpoint. Nothing is retried automatically.
"""),
    ("What never runs automatically", """
- **Placing, modifying, cancelling or exiting orders** (`PlaceOrder`, `ModifyOrder`, `CancelOrder`,
  `ExitSNOOrder` and anything similar) — blocked in code, always, on a live account.
- **The token exchange** (`GenAcsTok`) — a one-time login step, not a benchmark target.
- **Endpoints that need a fixture**, such as an existing order number, until you supply it.

For order endpoints, record timings you measured elsewhere with the **manual entry** form on the
Endpoint registry page. Manual and synthetic data are always labelled and kept apart from
automated measurements.
"""),
    ("WebSocket numbers are different", """
The WebSocket page measures connection, acknowledgement and first-message times, plus the gaps
between arriving messages. **Message arrival intervals are not exchange-to-client latency** —
quiet instruments simply tick less often. We never claim one-way network or exchange latency.
"""),
]

nav_col, body_col = st.columns([1, 3], gap="large")
with nav_col:
    st.markdown("**On this page**")
    st.markdown("\n".join(f"- [{title}](#m-{_i})" for _i, (title, _) in enumerate(SECTIONS)))
with body_col:
    for i, (title, body) in enumerate(SECTIONS):
        st.markdown(f'<a id="m-{i}"></a>', unsafe_allow_html=True)
        st.subheader(title, anchor=False)
        st.markdown(body.strip())
