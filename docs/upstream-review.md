# Upstream review

Nifty.AI was reviewed at [c48c624](https://github.com/imshashwataggarwal/nifty.ai/tree/c48c6249a58a9018a2a8a819bff499a8055f866b). Its MIT license permits reuse subject to retaining the notice. Its manifest includes TradingAgents, LangGraph, LangChain, OpenAlgo, HTTPX, Tenacity, MCP, DuckDB, pandas, and numerical and reporting dependencies. Phase 1 adopts no upstream code or dependencies. The review focused on the relevant source paths and runtime assumptions, without running upstream execution.

| Nifty.AI module | Decision | Source review |
| --- | --- | --- |
| `examples/strategies/ema_cross.py` | Reference Only | The example uses endpoint crossings of pandas exponentially weighted closes and couples the long entry to order sizing. Our EMA uses incremental state, a warmup, and explicit flat targets. |
| `engine/event_loop.py`, `engine/broker_sim.py` | Reference Only | The loop slices candles through the bar timestamp, then simulates fills against that same bar. Strategies using current-bar data can therefore receive fills priced or triggered by already observed OHLC values. Our replay waits for a subsequent quote. |
| `engine/portfolio.py` | Reference Only | The upstream module focuses on accounting and P&L. Phase 1 uses independently marked gross exposure and reconciles signed filled units against the broker ledger. |
| `openalgo/calendar.py`, `openalgo/symbols.py` | Reference Only | Calendar and derivative symbol conventions may be useful once licensed, instrument-aware feeds are introduced. Phase 1 uses fixture equity symbols and IANA time zones. |
| `openalgo/client.py`, `paper/runner.py` | Reject | The shared POST helper retries rate-limit responses without distinguishing order submission from read operations. Its HTTP exception wrapper also changes the exception type seen by the retry policy. The paper runner checks Analyzer Mode at startup, then submits through the same placement client. These paths do not establish the durable, independently reconciled intent contract required here. |
| Agent and MCP integrations | Reject | They add model, tool and network dependencies outside the Phase 1 execution boundary. News replay is first-party. |

OpenAlgo was reviewed at [11b5322](https://github.com/marketcalls/openalgo/tree/11b53225e8c7adf3103f060033b04c9add1125e5). The server is AGPL-3.0 and includes a Flask REST stack with broker-specific integrations. The reviewed `restx_api/place_order.py`, `orderbook.py` and `positionbook.py` routes accept API keys and provide distinct placement and reconciliation operations.

| OpenAlgo module | Decision | Source review |
| --- | --- | --- |
| Placement, order book and position book contracts | Reference Only | These define an integration boundary for a later phase. The route signatures alone cannot establish safe retry behavior after a lost response or broker-specific outcomes. |
| Server, broker implementations and credential handling | Reject | Phase 1 uses a local transactional fake broker and a disabled adapter stub. A later OpenAlgo deployment requires separate credential, contract and licensing review. |
