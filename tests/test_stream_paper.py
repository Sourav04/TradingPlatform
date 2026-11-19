from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.backtesting.historical import Settings, backtest
from trading_platform.core.models import Quote
from trading_platform.execution.store import Store
from trading_platform.market_data.groww_stream import (
    consume_sdk,
    decode_ltp,
    exchange_token,
)
from trading_platform.operations.stream_paper import (
    StreamState,
    approve_stream,
    disconnect,
    ingest,
    process_tick,
    stream_overview,
)
from trading_platform.operations.workspace import Paper, evidence_for, parse_csv

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2025, 1, 6, 3, 45, tzinfo=UTC)
SETTINGS = Settings(
    "NSE:WIPRO",
    date(2025, 1, 6),
    date(2025, 1, 17),
    stop_loss_percent=Decimal(2),
    max_risk_percent=Decimal(1),
    max_daily_loss_percent=Decimal(2),
)


def tick(offset: int, price: int = 100) -> Quote:
    return Quote(SETTINGS.symbol, START + timedelta(seconds=offset), Decimal(price))


def initial_state() -> StreamState:
    cash = SETTINGS.initial_cash
    paper = Paper(
        "demo",
        "evidence",
        START.isoformat(),
        SETTINGS.strategy_id,
        SETTINGS.strategy_version,
        SETTINGS.symbol,
        "active",
        0,
        0,
        0,
        cash,
        cash,
        Decimal(0),
        Decimal(0),
        0,
        None,
        None,
        [],
        [],
        [],
    )
    return StreamState(paper, daily_start=cash)


def ready_to_cross() -> StreamState:
    state = initial_state()
    for index, price in enumerate([100, 100, 98, 96, 95, 97, 101]):
        for offset in range(0, 300, 15):
            observation = tick(index * 300 + offset, price)
            state = process_tick(
                state, observation, observation.timestamp, SETTINGS, False
            )
    assert state.paper.fills == []
    return state


def test_paper_fills_at_next_observed_tick_and_stop_runs_while_paused() -> None:
    state = ready_to_cross()
    opening = tick(2100, 103)
    state = process_tick(state, opening, opening.timestamp, SETTINGS, False)
    assert state.paper.units == 10 and state.paper.stop == Decimal("100.94")
    assert state.paper.fills[0].fill_bar == opening.timestamp
    assert state.paper.fills[0].signal_bar < opening.timestamp
    assert state.candle and state.candle.volume == 0

    paused = replace(state, paper=replace(state.paper, status="paused"))
    late_quote = tick(2115, 96)
    stale = process_tick(
        paused, late_quote, late_quote.timestamp + timedelta(seconds=31), SETTINGS, True
    )
    assert stale == paused
    fresh_quote = tick(2160, 94)
    result = process_tick(paused, fresh_quote, fresh_quote.timestamp, SETTINGS, True)
    assert result.paper.units == 0 and result.ema is None
    assert result.paper.fills[-1].reason == "stop_loss"
    assert result.paper.fills[-1].execution_price == Decimal(94)
    assert result.paper.realized < 0
    assert (
        process_tick(result, fresh_quote, fresh_quote.timestamp, SETTINGS, False)
        == result
    )


def test_disconnect_and_pause_do_not_create_new_entries() -> None:
    state = ready_to_cross()
    opening = tick(2100, 103)
    disconnected = process_tick(
        replace(state, disconnected=True), opening, opening.timestamp, SETTINGS, False
    )
    assert not disconnected.paper.fills and disconnected.ema is None
    paused = replace(state, paper=replace(state.paper, status="paused"))
    outcome = process_tick(paused, opening, opening.timestamp, SETTINGS, False)
    assert not outcome.paper.fills and outcome.paper.rejections
    after_hours = Quote(SETTINGS.symbol, START.replace(hour=12), Decimal(100))
    assert (
        process_tick(state, after_hours, after_hours.timestamp, SETTINGS, False)
        == state
    )


def test_sdk_subscription_and_exchange_token(tmp_path: Path) -> None:
    file = tmp_path / "instruments.csv"
    file.write_text(
        "exchange,segment,trading_symbol,exchange_token\n"
        "BSE,CASH,WIPRO,1\nNSE,CASH,WIPRO,2\nNSE,FNO,WIPRO,3\n"
    )
    instrument = exchange_token(file, SETTINGS.symbol)
    assert instrument == "2"
    payload = {
        "ltp": {"NSE": {"CASH": {"2": {"tsInMillis": 1736135100000.0, "ltp": 103.1}}}}
    }
    updates: list[Quote] = []

    class Stream:
        def __init__(self, _: object) -> None:
            self.callback: Callable[[object], None] | None = None

        def subscribe_ltp(
            self,
            items: list[dict[str, str]],
            on_data_received: Callable[[object], None],
        ) -> None:
            assert items == [
                {"exchange": "NSE", "segment": "CASH", "exchange_token": "2"}
            ]
            self.callback = on_data_received

        def get_ltp(self) -> object:
            return payload["ltp"]

        def consume(self) -> None:
            assert self.callback is not None
            self.callback(None)

    class SDK:
        GrowwAPI = staticmethod(lambda _: object())
        GrowwFeed = Stream

    consume_sdk(SDK, "mock", SETTINGS.symbol, instrument, updates.append)
    assert len(updates) == 1 and updates[0].price == Decimal("103.1")
    assert decode_ltp(payload, SETTINGS.symbol, "2") == updates[0]
    assert decode_ltp(payload, SETTINGS.symbol, "3") is None


def test_stream_approval_persists_and_blocks_replay_deployment(store: Store) -> None:
    with TestClient(create_app(store)) as client:
        daily = (ROOT / "examples/ema-candles.csv").read_text()
        candles = parse_csv(daily)
        evidence = evidence_for(
            store, daily, backtest(candles, SETTINGS), candles, False
        )
        denied = client.post(
            "/operations/stream-paper", json={"evidence_id": evidence.id}
        )
        assert denied.status_code == 409 and "five-minute" in denied.json()["detail"]
        header, *rows = daily.splitlines()
        sample = (
            header
            + "\n"
            + "\n".join(
                (START + timedelta(minutes=5 * index)).isoformat()
                + ","
                + row.split(",", 1)[1]
                for index, row in enumerate(rows)
            )
        )
        candles = parse_csv(sample)
        evidence = evidence_for(
            store, sample, backtest(candles, SETTINGS), candles, False
        )
        approved = approve_stream(store, evidence.id, START)
        assert approved.feed_status == "offline"
        assert (
            client.post(
                "/operations/paper", json={"evidence_id": evidence.id}
            ).status_code
            == 409
        )
        first = tick(0)
        ingest(store, first, first.timestamp)
        assert stream_overview(store, first.timestamp)[0].feed_status == "fresh"
        assert (
            stream_overview(store, first.timestamp + timedelta(seconds=31))[
                0
            ].feed_status
            == "stale"
        )
        disconnect(store, SETTINGS.symbol)
        assert (
            client.get("/operations/stream-paper").json()[0]["feed_status"] == "offline"
        )
        assert client.get("/operations").json()["live_orders"] == []
    with TestClient(create_app(store)) as client:
        assert (
            client.get("/operations/stream-paper").json()[0]["paper"]["id"]
            == approved.paper.id
        )
