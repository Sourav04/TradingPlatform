# Trading operations dashboard

This branch builds on the backtesting dashboard in PR #4. Approval applies once per exact strategy deployment. The paper runner consumes historical candles one at a time.

| Work item | Status |
| --- | --- |
| Code-backed strategy catalog | Implemented |
| Persistent backtest evidence, chronological validation, and cost stress test | Implemented |
| Shared risk limits, paper approval and replay, reconciled fills, open positions, P&L | Implemented |
| Official news signals and advisory recommendations | Implemented |
| Live operations view and hard-disabled execution | Implemented |
| Persistent automatic backtest queue and evidence | Verified on SQLite, PostgreSQL and Chromium |
| Browser, SQLite and PostgreSQL verification | Passed. Chromium workflow and 34 tests per database. |
| Groww automatic streaming paper and feed status | Verified with mocks and Chromium. Broker entitlement check pending. |
| Streaming session overview and paper performance history | Verified on SQLite, PostgreSQL and Chromium |
| Unified order trace and company-specific disclosure outcome tracking | Future milestone |
| Authenticated multi-user approvals | Future milestone |
| Real OpenAlgo strategy import and live market/broker certification | Future milestone |
