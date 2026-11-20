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

## Dashboard, simulated stops, and official news

Start the API and open [the local dashboard](http://127.0.0.1:8000/dashboard).

```sh
uv sync --locked
uv run --locked uvicorn trading_platform.api.app:app
```

Select the synthetic sample or upload a single-symbol OHLCV CSV. Enter the symbol and date range, then run the backtest to see equity, P&L, drawdown, trades, risk rejections, and news available at candle opens during the run.

Every dashboard buy fill has a simulated stop. The default price stop is 2% and can be edited. The workspace uses a shared 1% estimated equity risk budget per trade. An entry is rejected if its modeled loss at the stop plus fees and slippage exceeds its risk budget. If the candle opens below an existing stop, the simulator exits at the open. Otherwise a candle low at or below the stop triggers an exit at the stop. A stop can trigger on the entry candle. Gap and slippage losses can exceed the risk budget. The engine assumes a full fill and does not infer finer price paths from OHLC data. The CLI supports the same controls with `--stop-loss-percent` and `--max-risk-percent`.

Record public press releases on demand using the same `TRADING_DATABASE_URL` as the dashboard:

```sh
uv run --locked python -m trading_platform.news_engine.official --source all
```

The collector uses the RSS feeds published by the [Reserve Bank of India](https://www.rbi.org.in/Scripts/rss.aspx) and the [Federal Reserve](https://www.federalreserve.gov/feeds/feeds.htm). It stores publication time and first observed time, and repeated polls retain the original observation. Uploaded CSVs show recorded events. The synthetic sample shows labeled mock news. The replay applies the 24-hour freshness and ingestion cutoff at each eligible candle open. News is displayed alongside price results. It has no bearing on EMA signals or execution.

## Trading operations workspace

Open [the local dashboard](http://127.0.0.1:8000/dashboard) to view the code-backed strategy catalog, run and save evidence, approve paper replay, and review recorded official news. The workspace is intended for one local operator. Keep the API bound to loopback.

A saved backtest retains its exact CSV, hash, strategy version, configuration and results. It also reruns a later date window when there are enough dates, and a double-slippage scenario when the cost settings allow. The later window is a chronological check. Use a separate dataset for an independent out-of-sample test. Evidence is eligible for paper replay only. The current code registry contains the in-repository EMA crossover.

Approve a specific evidence record once. The local paper account allows one deployment at a time, with a shared 1% estimated risk per trade, 2% daily loss limit, and ₹100,000 gross exposure cap. Advance one historical candle at a time. Pausing blocks new entries while later candles can still trigger protective exits. Closing flattens any open paper position at the last recorded candle close with modeled costs. Orders and P&L in this workflow are simulated from candle data. An uploaded dataset can be saved and replayed without Groww credentials.

The news view shows fresh official releases, first observation time, and advisory recommendations. The live view reports that live execution is disabled. Real-time paper feeds, external strategy imports and live broker certification are tracked in [the dashboard checklist](docs/dashboard-progress.md).

## Automatic backtests

In the dashboard, choose a CSV, strategy, date range, and parameters, then select **Enable automatic backtests**. This registers the validated dataset and configuration. With the API running, a background worker executes the queued job and saves the same evidence used by manual backtests. The dashboard shows queued, running, completed and failed jobs. A completed run does not approve a deployment.

Re-registering identical inputs returns the existing job. A changed CSV or configuration queues a distinct run. A registered plan also follows new versions of its strategy and the evidence suite. Jobs pin the exact version, dataset hash and settings. The worker checks for new versions about every 15 seconds and recovers abandoned claims after a five-minute lease. Avoid running different application versions against the same local workspace database.

The existing Groww downloader produces the same CSV format for registration. Groww live streaming uses the separate optional streaming collector below. Live order execution remains disabled.

## Streaming paper with Groww

Streaming paper uses an approved evidence record, live Groww NSE cash prices and simulated fills. Start the dashboard, upload and backtest an authorized regular-session five-minute CSV for one NSE cash symbol, then select **Approve Groww paper stream** on that evidence. Close any existing paper deployment before approving another. Set the collector and dashboard to the same `TRADING_DATABASE_URL`. With the local Python setup, both use the same SQLite file when run from the checkout.

Download the official Groww instrument master CSV and supply its local path. The collector resolves an exact NSE cash symbol to its exchange token. Set `GROWW_ACCESS_TOKEN` locally as described above and run during market hours:

```sh
GROWW_ACCESS_TOKEN="$GROWW_ACCESS_TOKEN" uv run --locked --with growwapi==1.5.0 \
  python -m trading_platform.market_data.groww_stream \
  --symbol NSE:WIPRO --instruments /path/to/groww-instruments.csv
```

To check market-data access before approving a paper deployment, add `--probe`. This prints received quotes with timestamps and does not touch the database. Stop the probe with Ctrl+C.

The dashboard polls for feed status, completed five-minute candle count, positions, simulated fills and P&L. The initial partial candle is discarded for signals. A disconnect or more than 30 seconds between ticks resets EMA warmup. Stale, duplicate and out-of-order ticks cannot execute fills. The feed process retries up to three connection attempts when the SDK returns or raises. If updates stop silently, the dashboard marks the feed stale. LTP-derived candles have no exchange volume and do not imply that every trade was observed.

Entries fill at an observed tick after a completed candle. Stops and closing exits use fresh observed ticks, including after a gap or while entries are paused. The runner targets a flat position in the final five minutes of the regular NSE session. If no fresh quote was available, it closes the remaining position at the first fresh tick of the next session. A feed outage can delay exits and increase simulated loss. The runner accepts regular NSE hours on weekdays and does not model special sessions. The implementation only subscribes to market data and never calls a Groww order endpoint.

## Next milestones

- Confirm permitted data feeds and usage terms for each exchange and agency. Add ingestion persistence, exchange calendars and source-specific monitoring.
- Expand realistic fill modeling, migrations and operational controls before any broker sandbox integration.
- Verify broker acknowledgment, idempotency and reconciliation contracts independently with a sandbox. Evaluate regulatory and authorization requirements before considering live execution.
- Explore AI-assisted analysis as a read-only consumer of validated data and simulation results.

See [upstream review](docs/upstream-review.md) for licensing and reuse decisions.
