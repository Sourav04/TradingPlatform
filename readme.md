initial file

# TradingPlatform

Phase 1 is a local simulator for Indian algorithmic trading. It runs an EMA crossover on deterministic market data, evaluates risk, submits to a persistent fake broker, reconciles fills and positions, and replays mock Indian and US news. Live trading is disabled. The repository requires no Groww credentials, OpenAlgo installation, or News MCP.

## Run locally

With Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv sync --locked
uv run --locked uvicorn trading_platform.api.app:app
```

This creates `simulation.db` in the working directory. To run the same service with PostgreSQL:

```sh
docker compose up --build --wait
```

Compose binds the API to loopback and gives PostgreSQL a named volume. Its defaults are for local simulation. Set `SIMULATION_PORT` to change the host port. Outside Compose, set `TRADING_DATABASE_URL` to a SQLAlchemy PostgreSQL or file-backed SQLite URL. Run one application process for initial schema creation. Tables are created on startup.

```sh
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/strategies
curl -fsS -X POST http://127.0.0.1:8000/simulations/ema
```

The fixture produces an entry and an exit, both filled on the next available quote, with zero final exposure and six validated news events at the default replay cutoff. Repeating the same request returns the recorded order outcomes. The cost estimate combines fees and slippage in basis points on simulated filled notional. The HTTP API accepts no custom orders, strategy code, broker configuration, or kill-switch commands.

## Architecture

| Module | Responsibility |
| --- | --- |
| `core` | Configuration, UTC contracts, clock and logging |
| `market_data` | Quote contract and ordered, disconnectable fixture feed |
| `news_engine` | Source contracts, UTC normalization, provenance, overnight handling and replay |
| `strategies` | Strategy protocol and incremental EMA crossover |
| `backtesting` | Chronological quote replay, next-quote execution and cost estimates |
| `portfolio` | Gross exposure from positions and current marks |
| `risk` | Strategy allowlist, kill switch, quote age, long-only sizing and gross limits |
| `execution` | Desired positions, durable intent claims, state transitions and reconciliation |
| `brokers` | Transactional fake broker and disabled OpenAlgo adapter |
| `certification` | Readiness reporting backed by the ledger |
| `api` | Health, strategy discovery and fixture simulation |

Execution serializes order decisions on a database control row. It checks the broker position against recorded fills, applies independent risk checks, and commits the uniquely identified intent before dispatch. Dispatch reacquires the lock and rechecks risk. An outstanding intent blocks new orders, and ambiguous acknowledgments can only advance through broker lookup. The fake broker records acceptance, fills and acknowledgment visibility durably in the simulation database.

The persisted kill switch blocks dispatch. During recovery, enable the switch and reconcile. If an intent has no fake-broker record, `ExecutionService.recover_unsubmitted()` can abandon it under the database lock. Clearing the switch requires all outstanding orders to resolve and the two position ledgers to agree. This proof of absence depends on the fake broker sharing the transaction and cannot be used with an external broker. Following a rejection or abandonment, an unchanged desired target remains suppressed.

The news fixtures cover NSE, BSE, RBI, Federal Reserve, BLS and BEA. Native publication times use Asia/Kolkata or America/New_York and are stored in UTC. US evening releases can fall on the next India calendar date. Replay exposes a record from the later of observed ingestion and publication plus configurable latency, provided publication is at most 24 hours old. Set `TRADING_NEWS_LATENCY_SECONDS` to change that minimum latency. Deduplication uses source IDs and normalized headlines, with earliest availability determining which cross-source record is visible. The collectors emit fixtures only and make no network requests.

## Verification

```sh
uv run --locked pytest --junitxml=reports/sqlite.xml
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src tests
uv run --locked pip-audit --local --skip-editable
```

To repeat the integration suite, set `TEST_DATABASE_URL` to a PostgreSQL test database URL and run pytest. The test role needs permission to create temporary test schemas. CI publishes JUnit reports and audits dependencies and Git history.

## Phase 2 historical backtesting

Run the offline EMA backtester with the included synthetic daily candles:

```sh
uv run --locked python -m trading_platform.backtesting.cli examples/ema-candles.csv \
  --symbol NSE:DEMO --start 2025-01-06 --end 2025-01-17 \
  --fast 2 --slow 4 --units 10 > backtest.json
```

The sample produces two fills, net P&L of −₹102.94, and approximate maximum drawdown of 0.1114%. For an actual backtest, replace the sample with an OHLCV CSV from a source you are authorized to use and specify the matching symbol. Each file represents one symbol. Use the headers `timestamp,open,high,low,close,volume` and strictly increasing ISO 8601 **bar-open** timestamps with a UTC offset. Ensure inputs contain completed candles of a consistent interval.

Start and end dates are inclusive in India Standard Time. Earlier candles warm the EMA without creating orders. The EMA uses completed closes, and the next available in-range open is the first eligible fill. The signal's timestamp identifies its input candle by its opening time. Replay applies the position and exposure limits, a seven-day maximum signal-to-fill gap, and available cash including costs. The gross exposure check uses the next open as the reference mark.

Set `--cash`, `--fee-bps`, and `--slippage-bps` to override the default ₹100,000, 5 bps, and 10 bps. The JSON includes settings, signals, signed fills, fees, slippage, risk rejections, the bar-close equity curve, net P&L including unrealized P&L, and maximum drawdown measured from the highest recorded equity. Trade count counts fills. Open positions are marked at the last close, and a last-bar signal is reported as unfilled. The model assumes full fills at the reference open plus estimated costs. Data quality, corporate actions, liquidity, and actual exchange charges can materially change results.

Additional synthetic CSVs in `examples/mock-candles/` exercise cases beyond the sample round trip. Use `--symbol NSE:DEMO --start 2025-01-06 --end 2025-01-20` for all three.

| CSV | Expected outcome |
| --- | --- |
| `flat-market.csv` | No signals or fills |
| `missing-sessions.csv` | One stale-signal rejection after a ten-day gap |
| `open-position.csv` | One fill, 10 units held at the end, and ₹38.44 net mark-to-market P&L |

## Phase 3 Groww historical data

The standalone downloader makes one historical-data GET request for one NSE cash symbol and completed daily candles. Use a Groww account enabled for historical data and generate an **access token** following the official instructions. Keep your token in your local terminal. [Groww authentication](https://groww.in/trade-api/docs/curl) · [Historical candle contract](https://groww.in/trade-api/docs/curl/backtesting)

In Bash, from this checkout:

```bash
uv sync --locked
read -rsp 'Groww access token: ' GROWW_ACCESS_TOKEN; printf '\n'
GROWW_ACCESS_TOKEN="$GROWW_ACCESS_TOKEN" uv run --locked python -m trading_platform.market_data.groww_cli \
  --symbol NSE:WIPRO --start 2025-09-01 --end 2025-09-30 --output groww-wipro-sep-2025
unset GROWW_ACCESS_TOKEN
```

Choose an inclusive range of at most 180 dates ending before today in India. The output directory must be new. The CSV uses UTC timestamps. Timezone-free timestamps from Groww are interpreted as India local time, consistent with the market times in the vendor examples. Duplicate daily candles, wrong-interval responses and invalid OHLCV rows fail validation. Metadata includes the request range, coverage, the CSV's SHA-256 checksum, and possible missing weekdays. Check missing weekdays against the exchange calendar since the downloader does not classify holidays.

Backtest the saved data with:

```sh
uv run --locked python -m trading_platform.backtesting.cli \
  groww-wipro-sep-2025/candles.csv --symbol NSE:WIPRO \
  --start 2025-09-01 --end 2025-09-30
```

Spot-check the first and last candles and the open price used for any fill. Repeat against the same CSV to confirm identical output. The automated suite uses mock Groww responses, so it runs without an account or network access. Keep downloaded datasets local unless the applicable data terms permit sharing.

## Next milestones

- Confirm permitted data feeds and usage terms for each exchange and agency. Add ingestion persistence, exchange calendars and source-specific monitoring.
- Expand realistic fill modeling, migrations and operational controls before any broker sandbox integration.
- Verify broker acknowledgment, idempotency and reconciliation contracts independently with a sandbox. Evaluate regulatory and authorization requirements before considering live execution.
- Explore AI-assisted analysis as a read-only consumer of validated data and simulation results.

See [upstream review](docs/upstream-review.md) for licensing and reuse decisions.
