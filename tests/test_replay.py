from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.brokers.contracts import BrokerUnavailable, OpenAlgoAdapter, Order
from trading_platform.execution.store import Store
from trading_platform.market_data.feed import fixture_quotes
from trading_platform.news_engine.events import Source, publication_time, replay
from trading_platform.news_engine.fixtures import fixture_collectors
from trading_platform.strategies.ema import EMACrossover


def test_news_freshness_latency_deduplication_and_provenance() -> None:
    event = next(iter(fixture_collectors()[0].collect()))
    duplicate = replace(event, source=Source.BSE, headline=event.headline.upper() + "!")
    assert not list(replay([event], event.published_at + timedelta(minutes=1)))
    assert not list(replay([event], event.ingested_at, timedelta(minutes=3)))
    assert list(replay([event, duplicate], event.ingested_at)) == [duplicate]
    assert list(replay([event], event.published_at + timedelta(hours=24))) == [event]
    assert not list(
        replay([event], event.published_at + timedelta(hours=24, seconds=1))
    )
    with pytest.raises(ValueError, match="Ingestion"):
        replace(event, ingested_at=event.published_at - timedelta(seconds=1))


def test_us_overnight_and_daylight_saving() -> None:
    local = datetime(2025, 1, 6, 16)
    assert publication_time(Source.FED, local) == datetime(2025, 1, 6, 21, tzinfo=UTC)
    assert publication_time(Source.BLS, local.replace(month=7)).hour == 20
    event = next(iter(fixture_collectors()[3].collect()))
    assert event.india_date == "2025-01-07"
    assert not list(replay([event], datetime(2025, 1, 6, 20, tzinfo=UTC)))
    with pytest.raises(ValueError, match="explicit offset"):
        publication_time(Source.BEA, datetime(2025, 11, 2, 1, 30))


def test_ema_prefix_is_invariant_to_future_prices() -> None:
    prefix = [100, 98, 96, 95, 97, 101]

    def signals(prices: list[int]) -> list[int]:
        strategy = EMACrossover()
        return [
            signal.target
            for quote in fixture_quotes(prices)
            if (signal := strategy.on_quote(quote)) is not None
        ]

    assert signals(prefix) == [10]
    for suffix in ([105, 110, 130], [100, 95, 90]):
        assert signals(prefix + suffix)[:1] == signals(prefix)


def test_simulation_endpoint_is_reproducible(store: Store) -> None:
    with TestClient(create_app(store)) as client:
        assert client.get("/health").json()["live_trading"] is False
        assert client.get("/strategies").json() == ["ema-crossover"]
        first = client.post("/simulations/ema")
        assert first.status_code == 200
        payload = first.json()
        assert [signal["target"] for signal in payload["signals"]] == [10, 0]
        assert [item["order"]["status"] for item in payload["orders"]] == [
            "filled",
            "filled",
        ]
        assert all(
            item["quote_time"] > signal["quote"]["timestamp"]
            for item, signal in zip(payload["orders"], payload["signals"], strict=True)
        )
        assert [item["order"]["quantity"] for item in payload["orders"]] == [10, -10]
        assert [item["estimated_cost_inr"] for item in payload["orders"]] == [
            "1.56",
            "1.41",
        ]
        assert payload["positions"] == {}
        assert len(payload["news"]) == 6
        assert client.post("/simulations/ema").json() == payload
        assert client.post("/orders", json={}).status_code == 404


def test_openalgo_adapter_has_no_execution_transport() -> None:
    with pytest.raises(BrokerUnavailable, match="disabled"):
        OpenAlgoAdapter().submit(Order("id", "NSE:DEMO", 1))
