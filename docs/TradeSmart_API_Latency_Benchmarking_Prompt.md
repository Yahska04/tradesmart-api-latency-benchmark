# Project: TradeSmart API Latency Benchmarking Dashboard

Act as a senior Python developer, API performance engineer, and dashboard designer. Build a complete, locally hosted API latency benchmarking application for TradeSmart's API.

## 1. Project Objective

I want to measure the actual response latency of every available endpoint in TradeSmart's API V2 documentation:

Documentation: https://apidocs.tradesmartonline.in/v2/

The application should execute API requests, measure response times, store the results locally, and display actual P50, P95, and P99 latency for each endpoint.

This tool is intended for internal API performance analysis, debugging, and benchmarking. It must not place live trading orders or modify real client positions.

## 2. Technology Stack

Use the following technologies:

- Python 3.12 or a compatible stable version
- Streamlit for the user interface
- HTTPX for HTTP requests and timing
- Pandas and NumPy for data processing and percentile calculations
- SQLite for local storage of benchmark results
- Plotly for interactive charts
- Pytest for testing the application
- Python logging for error tracking

The application must run locally on my Windows computer using VS Code and Claude Code.

## 3. API Documentation Analysis

First, inspect the TradeSmart API V2 documentation at the URL provided above.

Identify and catalog all documented endpoints, including:

- Authentication and session management
- User and account information
- Funds and margin
- Order placement, modification, cancellation, and order book
- Trade book and positions
- Holdings and portfolio
- Quotes and market data
- Historical and time-series data
- WebSocket market data, if documented
- Any other available API categories

Do not assume the endpoint list above is exhaustive.

For every endpoint, record:

- API category
- Endpoint name
- HTTP method
- URL path
- Required headers
- Required parameters
- Required request body
- Authentication requirements
- Expected response format
- Whether the endpoint is safe to benchmark automatically
- Whether it requires a special test fixture or manual setup

Use the official documentation as the source of truth. Do not invent endpoints, request schemas, authentication formats, or response fields.

If the documentation cannot be accessed automatically, create an importable endpoint configuration and ask me to supply the relevant documentation or endpoint definitions. Continue building the rest of the application.

## 4. Authentication and Configuration

Create a Settings page where I can configure:

- API base URL
- API credentials or access token
- Static IP and other required settings, if applicable
- Request timeout
- Number of test iterations
- Delay between requests
- Warm-up requests
- Test duration
- Selected endpoints

Read the actual authentication process from the official documentation.

Never hardcode credentials in the source code. Store sensitive credentials in environment variables or a local .env file, exclude .env from Git, and provide a .env.example containing placeholder values only.

Mask secrets in the interface and logs.

Do not attempt to bypass authentication, rate limits, IP restrictions, or other API security controls.

## 5. Endpoint Safety Classification

Before executing tests, classify each endpoint as one of the following:

A. Read-only: Suitable for controlled automated latency tests, subject to rate limits.

B. State-changing: Includes order placement, modification, cancellation, and other operations that modify account state.

C. Special setup required: Includes endpoints requiring WebSocket sessions, particular account states, or additional test fixtures.

Automatically benchmark only approved read-only endpoints.

Never automatically place, modify, or cancel real orders. Never run state-changing endpoints without an explicit, separate test mode, a documented safe test environment, and user confirmation.

Provide a clear warning when a live account is selected.

For order-related endpoints, support documentation-only registration and manual latency result entry until a safe sandbox or mock environment is configured.

## 6. Latency Measurement Engine

Build a reusable benchmarking engine that measures the elapsed time for every individual request.

For each request, record:

- Endpoint name and path
- HTTP method
- Test run ID
- Request start timestamp
- Request completion timestamp
- Client-observed elapsed latency in milliseconds
- HTTP status code
- Success or failure
- Timeout status
- Connection or DNS error, if identifiable
- Response size in bytes
- Error message, with credentials and sensitive data redacted

Use a monotonic high-resolution clock, such as `time.perf_counter_ns()`, to measure elapsed request duration.

Measure the entire client-observed HTTP request lifecycle, including connection establishment when a new connection is required, network transit, server processing, and response receipt.

Also support a separate warm-connection test mode using HTTP connection reuse.

Do not label client-observed latency as pure server processing time. If server timing headers are available, record them separately without assuming that they represent complete server execution time.

Record failed requests and timeouts as failures. Do not silently discard them or treat them as successful low-latency requests.

## 7. Actual P50, P95, and P99 Latency

Calculate the following metrics separately for each endpoint and test run:

- Actual P50 latency
- Actual P95 latency
- Actual P99 latency
- Minimum latency
- Maximum latency
- Arithmetic mean latency
- Standard deviation
- Total requests
- Successful requests
- Failed requests
- Timeout count
- Success rate
- Requests per second, where applicable

Use the actual collected latency samples, not estimated or fabricated numbers.

Define the percentile calculation method explicitly and use a consistent NumPy percentile method, such as `method="linear"`.

Calculate percentiles from successful completed request durations and report the sample count used. Also report failures and timeouts separately so that percentile values cannot hide reliability problems.

If there are insufficient observations for a meaningful P99 interpretation, display a warning and the sample count. Do not imply that a small sample provides a reliable tail-latency estimate.

Show the exact date and time of the test, the number of requests, the test configuration, and the benchmark run ID alongside the metrics.

## 8. Dashboard Design

Build a clean, professional, modern Streamlit dashboard using TradeSmart-inspired branding.

The dashboard must include these sections.

### A. Overview

Display metric cards for:

- Number of endpoints tested
- Total requests executed
- Overall P50 latency
- Overall P95 latency
- Overall P99 latency
- Overall success rate
- Total failures and timeouts

Clearly explain that overall percentiles are calculated from the applicable individual request samples and may be affected by the mix of endpoints tested.

### B. Endpoint Performance Table

Create a sortable, searchable table with:

| Endpoint | Method | Samples | P50 (ms) | P95 (ms) | P99 (ms) | Min (ms) | Max (ms) | Mean (ms) | Success Rate |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|

Allow filtering by API category, HTTP method, status, and benchmark run.

Clicking an endpoint should open its detailed performance page.

### C. Latency Distribution

For each selected endpoint, display:

- Latency distribution histogram
- Box plot
- P50, P95, and P99 markers
- Latency over time
- Response status breakdown
- Failure and timeout history

### D. Endpoint Comparison

Allow users to select multiple endpoints and compare their P50, P95, and P99 latency.

Display comparison charts and a table.

Do not rank endpoints as faster or slower unless they were tested under sufficiently comparable conditions.

### E. Historical Performance

Show performance trends across multiple benchmark runs.

Allow comparison of two runs to identify:

- P50 latency change
- P95 latency change
- P99 latency change
- Success rate change
- Failure rate change

Display both absolute differences in milliseconds and percentage changes.

Handle zero baselines and missing data safely.

### F. Request-Level Results

Provide a detailed table containing individual request samples, timestamps, response times, status codes, and errors.

Add filters and CSV export.

Never expose access tokens, API secrets, account numbers, or sensitive response bodies in exported data.

## 9. Test Configuration

Allow the user to configure:

- Selected endpoints
- Number of iterations per endpoint
- Warm-up request count
- Timeout duration
- Delay between requests
- Connection reuse on or off
- Sequential testing mode
- Optional low-concurrency testing, disabled by default

Start with sequential testing. Do not run aggressive concurrent tests by default.

Read and respect the API's documented rate limits. Apply conservative limits, configurable request pacing, and a safety buffer below the documented maximum.

Stop the test or pause the affected endpoint when rate-limit responses, repeated errors, or unexpected behavior occur.

Do not retry failed requests automatically unless the retry policy is explicitly configured and safe for the endpoint. Do not automatically retry state-changing operations.

Show the planned number of requests and estimated test duration before starting a benchmark.

## 10. Database and Data Persistence

Use SQLite to store:

- Endpoint definitions
- Benchmark run metadata
- Individual request results
- Aggregated endpoint metrics
- Test configuration
- Application settings that do not contain secrets

Persist results so that closing and reopening the application does not delete historical data.

Create database tables with appropriate IDs, timestamps, and indexes.

Provide a way to export individual results and aggregated metrics to CSV.

## 11. WebSocket Latency

If the official documentation supports WebSocket market data, add a separate WebSocket benchmarking module.

Measure and report:

- Connection establishment time
- Subscription acknowledgement time, if identifiable
- Time to first received market-data message
- Inter-message arrival intervals
- Message counts
- Disconnects and reconnections
- Message gaps, where they can be reliably identified

Clearly distinguish observed message-arrival intervals from true exchange-to-client market-data latency.

Do not claim one-way network latency or exchange event latency unless trustworthy source timestamps and a defensible measurement method are available.

Keep WebSocket metrics separate from ordinary HTTP endpoint latency.

## 12. Benchmark Quality and Fairness

Include a methodology page explaining:

- Client-observed latency
- Cold connection versus warm connection
- P50, P95, and P99
- Sample size limitations
- Network variability
- Server load and market-session effects
- Authentication overhead
- HTTP status codes and failures
- Why results can differ between test runs

Include an optional connectivity check before starting.

Use consistent test conditions when comparing endpoints or historical runs. Record relevant test settings, connection mode, and timestamps.

Do not claim that the dashboard measures pure broker server latency. Clearly state that measured latency includes the components observable from the client.

## 13. Application Structure

Create a maintainable project structure similar to:

```text
tradesmart-api-benchmark/
    app.py
    requirements.txt
    .env.example
    .gitignore
    README.md
    config/
        endpoints.json
    src/
        api_client.py
        auth.py
        endpoint_registry.py
        benchmark_engine.py
        metrics.py
        database.py
        safety.py
        websocket_benchmark.py
        exporters.py
    pages/
        1_Endpoint_Registry.py
        2_Run_Benchmark.py
        3_Performance_Analytics.py
        4_Historical_Comparison.py
        5_Settings.py
        6_Methodology.py
    tests/
        test_metrics.py
        test_benchmark_engine.py
        test_safety.py
        test_database.py
    data/
        .gitkeep
```

You may improve this structure if necessary, but keep modules organized and responsibilities separate.

Do not commit credentials, benchmark databases containing sensitive information, or private API responses to Git.

## 14. Testing Requirements

Write automated tests covering:

- Percentile calculations against known sample datasets
- Empty datasets and insufficient sample sizes
- Failed requests and timeouts
- Success-rate calculations
- Database persistence
- Endpoint configuration loading
- Request timing accuracy
- Rate-limit enforcement
- Prevention of unsafe state-changing requests
- Secret redaction in logs and exports

Use mocked HTTP responses for automated unit tests so they do not accidentally send requests to TradeSmart's production API.

Include a demo mode with clearly labelled synthetic sample data so I can explore the dashboard without credentials. Never mix synthetic data with actual results without a visible distinction.

## 15. Installation and Run Instructions

Create a detailed README with Windows instructions for VS Code:

1. Verify Python and install dependencies.
2. Create and activate a virtual environment.
3. Configure the `.env` file.
4. Configure or review the endpoint registry.
5. Run automated tests.
6. Start the Streamlit application.
7. Open the local dashboard.
8. Run a small benchmark against approved read-only endpoints.
9. Review P50, P95, and P99 results.
10. Export the results to CSV.

Provide the exact PowerShell commands.

## 16. Implementation Process

Work in the following order:

Phase 1: Inspect the documentation and create the endpoint registry.

Phase 2: Build the API client, authentication configuration, and endpoint safety controls.

Phase 3: Implement accurate request timing and percentile calculations.

Phase 4: Implement SQLite persistence and historical run tracking.

Phase 5: Build the Streamlit dashboard and charts.

Phase 6: Add CSV export, optional WebSocket testing, and demo mode.

Phase 7: Run automated tests, fix errors, and document limitations.

Start by inspecting the current workspace and documentation. Then implement the application in the current VS Code project folder.

Do not merely provide sample code or pseudocode. Create the actual working project files, install dependencies only when appropriate, run the tests, fix errors, and provide clear commands to launch the app.

If a required detail is missing, implement the components that can be completed safely and identify exactly what configuration or information is still needed.

## Final Acceptance Criteria

The application is complete when:

1. It can list and manage the documented TradeSmart API endpoints.
2. It can benchmark approved read-only endpoints using real HTTP requests.
3. It records individual observed latency samples.
4. It calculates actual P50, P95, and P99 from those samples.
5. It shows success rates, failures, timeouts, and sample counts.
6. It persists benchmark history locally.
7. It compares endpoints and historical runs.
8. It exports actual results to CSV.
9. It prevents accidental live order placement.
10. It runs locally on Windows using VS Code and Streamlit.
11. It passes its automated tests.
12. It clearly distinguishes real measurements from synthetic demo data.

Prioritize measurement accuracy, safety, transparency, and maintainable code over visual effects.
