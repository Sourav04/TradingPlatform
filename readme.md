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

## Next milestones

- Confirm permitted data feeds and usage terms for each exchange and agency. Add ingestion persistence, exchange calendars and source-specific monitoring.
- Expand realistic fill modeling, migrations and operational controls before any broker sandbox integration.
- Verify broker acknowledgment, idempotency and reconciliation contracts independently with a sandbox. Evaluate regulatory and authorization requirements before considering live execution.
- Explore AI-assisted analysis as a read-only consumer of validated data and simulation results.

See [upstream review](docs/upstream-review.md) for licensing and reuse decisions.
